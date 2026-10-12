from __future__ import annotations

import asyncio
import sqlite3
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from db import AsyncDatabase, Database
from db.async_common import AsyncConnection
from errors import CodeError, DatabaseError, DuplicateError, MigrationError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from _pytest.monkeypatch import MonkeyPatch

    from db.async_common import Parameters


@pytest_asyncio.fixture
async def repository(tmp_path: Path) -> AsyncGenerator[AsyncDatabase]:
    db = AsyncDatabase(path=tmp_path / "state.sqlite3", backup_dir=tmp_path / "backups", start_backup=False)
    await db.start()
    try:
        yield db
    finally:
        await db.close()


async def create_user(db: AsyncDatabase, name: str = "alice") -> None:
    await db.create_user(username=name, uuid=f"uuid-{name}", token=f"token-{name}", fingerprint="chrome", displayname=name, ext_username=f"{name}@test", ext_password_hash="hash")


@pytest.mark.asyncio
async def test_users_auth_mapping_and_errors(repository: AsyncDatabase) -> None:
    db = repository
    await create_user(db)
    await create_user(db, "bob")
    with pytest.raises(DuplicateError):
        await create_user(db)
    assert await db.list_users() == await db.all_users() == ["alice", "bob"]
    assert await db.usernames() == {"alice", "bob"}
    assert await db.username_exts() == {"alice": "alice@test", "bob": "bob@test"}
    await db.set_auth_token("alice", "session")
    assert await db.auth_token_to_user("session") == "alice"
    assert await db.token_to_user("token-alice") == "alice"
    assert await db.ext_to_user("alice@test") == "alice"
    assert await db.user_to_ext("alice") == "alice@test"
    assert await db.ext_password("alice@test") == "hash"
    await db.set_user("alice", tgid=12, status=False, bw_limit=3)
    await db.set_telegram("bob", "12")
    assert await db.user_to_tgid("alice") is None
    assert await db.tgid_to_user(12) == "bob"
    assert await db.user_tgids() == {"bob": "12"}
    await db.set_telegram_language(12, "en")
    assert await db.get_telegram_language(12) == "en"
    assert await db.has_telegram_language(12)
    assert not await db.has_telegram_language(13)
    assert await db.get_telegram_language(13) == "ru"
    with pytest.raises(DatabaseError):
        await db.set_telegram_language(12, "bad")
    await db.update_user("alice", displayname="Alice")
    await db.delete_user("bob")
    assert not await db.user_exists("bob")
    assert await db.user_tgids() == {}
    with pytest.raises(DatabaseError):
        await db.set_auth_token("missing", "session")


@pytest.mark.asyncio
async def test_atomic_registration_race_and_refund(repository: AsyncDatabase) -> None:
    db = repository
    await db.add_code("once", "register", gb=4, wl_gb=2)

    async def register(name: str) -> object:
        return await db.register_with_code(code="once", username=name, uuid=name, token=name, fingerprint="chrome", displayname=name, ext_username=name, ext_password_hash="hash")

    results = await asyncio.gather(register("alice"), register("bob"), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, CodeError) for result in results) == 1
    winner = (await db.list_users())[0]
    assert await db.get_code("once") is None
    await db.rollback_registration_sync(winner)
    assert await db.list_users() == []
    assert (await db.get_code("once")) is not None
    await register("carol")
    await db.confirm_registration_sync("carol")
    assert await db.all_codes() == []


@pytest.mark.asyncio
async def test_bonus_usage_notifications_and_snapshots(repository: AsyncDatabase) -> None:
    db = repository
    await create_user(db)
    await db.add_code("bonus", "bonus", gb=3, wl_gb=2, days=1)
    results = await asyncio.gather(db.adjust_bonus("alice", "bonus"), db.adjust_bonus("alice", "bonus"), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, CodeError) for result in results) == 1
    user = await db.get_user("alice")
    assert user is not None
    assert user["bw_limit_gb"] == 3
    assert user["wl_limit_gb"] == 2
    assert user["expires_at"] == 0
    await db.increment_usage("alice", regular=4)
    await db.increment_usages({"alice": (2, 3)})
    await db.mark_notification("legacy", 12)
    await db.sync_notifications(set(), {"marker"})
    await db.sync_notifications({"marker"}, {"new"})
    assert await db.notification_markers() == {"new"}
    assert await db.notification_seen("legacy", 12)
    assert not await db.mark_notification("legacy", 12)
    await db.add_bandwidth_snapshot("alice", 10, 1, 2, 3, 4)
    await db.add_bandwidth_snapshots([("alice", 10, 2, 3, 4, 5)])
    snapshots = await db.get_bandwidth_snapshots("alice", 0)
    assert snapshots[0]["up"] == 3
    assert snapshots[0]["wl_down"] == 9
    assert await db.prune_bandwidth_snapshots(11) == 1
    await db.upsert_state_snapshot(10, {"ts": 10, "host": {}, "panels": {}})
    assert (await db.get_state_snapshots(0))[0]["ts"] == 10
    assert await db.prune_state_snapshots(11) == 1
    assert await db.reset_monthly("2026-10", "2026-10-01")
    assert not await db.reset_monthly("2026-10", "2026-10-02")
    user = await db.get_user("alice")
    assert user is not None
    assert user["bw_used"] == user["wl_used"] == 0
    assert await db.notification_markers() == set()
    await db.set_metadata("prefix_%", "value")
    assert await db.list_metadata("prefix_") == {"prefix_%": "value"}
    await db.delete_metadata("prefix_%")
    await db.set_admin_ui_session("session")
    assert await db.admin_ui_session() == "session"
    await db.set_admin_ui_session(None)
    assert await db.admin_ui_session() is None


@pytest.mark.asyncio
async def test_cancelled_transaction_rolls_back_before_next_writer(repository: AsyncDatabase) -> None:
    entered = asyncio.Event()

    async def operation() -> None:
        async with repository.transaction(immediate=True) as conn:
            await conn.execute("INSERT INTO app_metadata VALUES ('cancelled', 'value')")
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(operation())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await repository.get_metadata("cancelled") is None
    await repository.set_metadata("later", "clean")
    assert await repository.get_metadata("later") == "clean"


@pytest.mark.asyncio
@pytest.mark.parametrize("commit", [False, True])
async def test_cancellation_during_queued_sql_settles(repository: AsyncDatabase, monkeypatch: MonkeyPatch, commit: bool) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    writer = repository._writer
    assert writer is not None
    execute = AsyncConnection._execute

    async def gated(self: AsyncConnection, sql: str, parameters: Parameters) -> int:
        if sql == ("COMMIT" if commit else "INSERT INTO app_metadata VALUES ('outcome', 'value')"):
            entered.set()
            await release.wait()
        return await execute(self, sql, parameters)

    monkeypatch.setattr(AsyncConnection, "_execute", gated)

    async def operation() -> None:
        async with repository.transaction(immediate=True) as conn:
            await conn.execute("INSERT INTO app_metadata VALUES ('outcome', 'value')")
    task = asyncio.create_task(operation())
    await entered.wait()
    task.cancel()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await repository.get_metadata("outcome") == ("value" if commit else None)
    assert not writer.in_transaction


@pytest.mark.asyncio
async def test_pragmas_leases_and_nested_rejection(repository: AsyncDatabase) -> None:
    async with repository.connection() as conn:
        assert (await conn.row("PRAGMA foreign_keys"))[0] == 1  # type: ignore[index]
        assert (await conn.row("PRAGMA journal_mode"))[0] == "wal"  # type: ignore[index]
        with pytest.raises(DatabaseError, match="nested"):
            await repository.get_metadata("key")
    with pytest.raises(DatabaseError):
        async with repository.connection() as conn:
            await conn.execute("INSERT INTO app_metadata VALUES ('bad', 'bad')")
    await repository.set_metadata("healthy", "yes")


@pytest.mark.asyncio
async def test_backup_restores_and_close_is_repeatable(repository: AsyncDatabase, tmp_path: Path) -> None:
    await create_user(repository)
    await repository.backup_now()
    backup = next((tmp_path / "backups" / "state").glob("*.sqlite3"))
    restored = AsyncDatabase(path=backup, start_backup=False)
    await restored.start()
    try:
        assert await restored.list_users() == ["alice"]
    finally:
        await restored.close()
    await repository.close()
    await repository.close()
    with pytest.raises(DatabaseError):
        await repository.get_metadata("key")


@pytest.mark.asyncio
async def test_existing_schema_and_failed_start_unwind(tmp_path: Path) -> None:
    path = tmp_path / "existing.sqlite3"
    sync = Database(path=path, start_backup=False)
    sync.set_metadata("existing", "preserved")
    sync.close()
    db = AsyncDatabase(path=path)
    await db.start()
    assert await db.get_metadata("existing") == "preserved"
    await db.close()
    bad_path = tmp_path / "bad.sqlite3"
    with sqlite3.connect(bad_path) as conn:
        conn.execute("CREATE TABLE unknown (id INTEGER)")
    bad = AsyncDatabase(path=bad_path)
    with pytest.raises(MigrationError):
        await bad.start()
    assert bad._writer is None
    assert not bad._readers
    await bad.close()


@pytest.mark.asyncio
async def test_rollback_failure_discards_writer(repository: AsyncDatabase, monkeypatch: MonkeyPatch) -> None:
    original = AsyncConnection._execute
    old_writer = repository._writer

    async def broken(self: AsyncConnection, sql: str, parameters: Parameters) -> int:
        if sql == "ROLLBACK":
            raise sqlite3.OperationalError("injected rollback failure")
        return await original(self, sql, parameters)

    monkeypatch.setattr(AsyncConnection, "_execute", broken)

    async def operation() -> None:
        async with repository.transaction(immediate=True) as conn:
            await conn.execute("INSERT INTO app_metadata VALUES ('failed', 'value')")
            raise CodeError("abort")

    with pytest.raises(CodeError):
        await operation()
    assert repository._writer is None
    await repository.set_metadata("new", "value")
    assert repository._writer is not old_writer
    assert await repository.get_metadata("failed") is None


@pytest.mark.asyncio
async def test_readers_do_not_see_uncommitted_writer_and_close_drains(repository: AsyncDatabase) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def write() -> None:
        async with repository.transaction(immediate=True) as conn:
            await conn.execute("INSERT INTO app_metadata VALUES ('pending', 'value')")
            entered.set()
            await release.wait()

    task = asyncio.create_task(write())
    await entered.wait()
    assert await repository.get_metadata("pending") is None
    closing = asyncio.create_task(repository.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await task
    await closing
    restored = AsyncDatabase(path=repository.path)
    await restored.start()
    try:
        assert await restored.get_metadata("pending") == "value"
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_code_duplicates_and_failed_registration_preserve_code(repository: AsyncDatabase) -> None:
    await create_user(repository)
    await repository.add_code("register", "register")
    with pytest.raises(DuplicateError):
        await repository.add_code("register", "register")
    with pytest.raises(DuplicateError):
        await repository.register_with_code(code="register", username="alice", uuid="new", token="new", fingerprint="chrome", displayname="Alice", ext_username="new", ext_password_hash="hash")
    code = await repository.get_code("register")
    assert code is not None
    assert code["uses"] == 1
    await repository.add_code("permanent", "bonus", permanent=True)
    assert (await repository.consume_code("permanent", "bonus"))["uses"] == -1
    assert await repository.delete_code("permanent")
    assert not await repository.delete_code("permanent")
