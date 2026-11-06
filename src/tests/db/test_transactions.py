from __future__ import annotations

import asyncio
import sqlite3
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from api.application import create_api_application
from db import Database
from db.common import Connection
from errors import CodeError, DatabaseError, DuplicateError, MigrationError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from _pytest.monkeypatch import MonkeyPatch

    from db.common import Parameters


@pytest_asyncio.fixture
async def repository(tmp_path: Path) -> AsyncGenerator[Database]:
    db = Database(
        path=tmp_path / "state.sqlite3",
        backup_dir=tmp_path / "backups",
        start_backup=False,
    )
    await db.start()
    try:
        yield db
    finally:
        await db.close()


async def create_user(db: Database, name: str = "alice") -> None:
    await db.create_user(
        username=name,
        uuid=f"uuid-{name}",
        token=f"token-{name}",
        fingerprint="chrome",
        displayname=name,
        ext_username=f"{name}@test",
        ext_password_hash="hash",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("lease", ["connection", "transaction"])
@pytest.mark.parametrize(
    ("code", "status"),
    [
        (sqlite3.SQLITE_BUSY, 503),
        (sqlite3.SQLITE_LOCKED, 503),
        (sqlite3.SQLITE_CANTOPEN, 503),
        (sqlite3.SQLITE_CORRUPT, 500),
        (sqlite3.SQLITE_IOERR, 500),
        (sqlite3.SQLITE_ERROR, 500),
    ],
)
async def test_database_failures_have_safe_http_responses(
    repository: Database,
    lease: str,
    code: int,
    status: int,
) -> None:
    cause = sqlite3.OperationalError("private/path.sqlite: UNIQUE secret SQL details")
    cause.sqlite_errorcode = code
    caught: list[DatabaseError] = []
    app = create_api_application()

    @app.get("/database-failure")
    async def fail() -> str:
        try:
            async with getattr(repository, lease)():
                raise cause
        except DatabaseError as exc:
            caught.append(exc)
            raise

    response = await app.test_client().get("/database-failure")
    assert response.status_code == status
    body = await response.get_data(as_text=True)
    assert "Database" in body
    assert "secret" not in body
    assert "private/path" not in body
    assert "UNIQUE" not in body
    assert caught[0].__cause__ is cause
    assert "secret SQL details" in str(caught[0])
    assert await repository.list_users() == []


@pytest.mark.asyncio
async def test_real_lock_and_corrupt_startup_have_server_status(
    repository: Database,
    tmp_path: Path,
) -> None:
    repository.timeout = 0.001
    async with repository.transaction() as conn:
        await conn.execute("PRAGMA busy_timeout = 1")
    blocker = sqlite3.connect(repository.path)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(DatabaseError) as locked:
            await create_user(repository)
        assert locked.value.status == 503
        assert "locked" not in locked.value.message
        assert isinstance(locked.value.__cause__, sqlite3.OperationalError)
    finally:
        blocker.close()
    await create_user(repository)

    path = tmp_path / "corrupt.sqlite3"
    path.write_text("not a database")
    corrupt = Database(path=path, start_backup=False)
    try:
        with pytest.raises(DatabaseError) as failure:
            await corrupt.start()
        assert failure.value.status == 500
        assert failure.value.message == "Database operation failed"
        assert isinstance(failure.value.__cause__, sqlite3.DatabaseError)
    finally:
        await corrupt.close()


@pytest.mark.asyncio
async def test_operational_error_is_not_mistaken_for_duplicate(
    repository: Database,
    monkeypatch: MonkeyPatch,
) -> None:
    original = Connection.execute
    cause = sqlite3.OperationalError("UNIQUE index unavailable: private details")

    async def execute(
        conn: Connection,
        sql: str,
        parameters: Parameters = (),
    ) -> int:
        if sql.startswith("INSERT INTO users"):
            raise cause
        return await original(conn, sql, parameters)

    monkeypatch.setattr(Connection, "execute", execute)
    with pytest.raises(DatabaseError) as failure:
        await create_user(repository)
    assert not isinstance(failure.value, DuplicateError)
    assert failure.value.status == 500
    assert failure.value.message == "Database operation failed"
    assert failure.value.__cause__ is cause


@pytest.mark.asyncio
async def test_reader_recovery_failure_is_sanitized(
    repository: Database,
    monkeypatch: MonkeyPatch,
) -> None:
    cause = sqlite3.OperationalError("cannot reopen private/path.sqlite")
    cause.sqlite_errorcode = sqlite3.SQLITE_CANTOPEN

    async def fail_open(*, reader: bool = False) -> None:
        raise cause

    monkeypatch.setattr(repository, "_open", fail_open)
    with pytest.raises(DatabaseError) as failure:
        async with repository.connection() as conn:
            await conn.execute("SELECT * FROM missing_private_table")
    assert failure.value.status == 503
    assert failure.value.message == "Database temporarily unavailable"
    assert failure.value.__cause__ is cause


@pytest.mark.asyncio
async def test_constraint_and_validation_errors_remain_client_errors(repository: Database) -> None:
    await create_user(repository)
    with pytest.raises(DuplicateError) as duplicate:
        await create_user(repository)
    assert duplicate.value.status == 400
    assert "already exists" in duplicate.value.message
    await repository.add_code("duplicate", "register")
    with pytest.raises(DuplicateError):
        await repository.add_code("duplicate", "register")
    with pytest.raises(CodeError) as invalid:
        await repository.consume_code("missing", "register")
    assert invalid.value.status == 400
    with pytest.raises(DatabaseError) as constraint:
        await repository.set_telegram_language(12, "bad")
    assert constraint.value.status == 400
    assert constraint.value.message == "Database constraint conflict"
    assert isinstance(constraint.value.__cause__, sqlite3.IntegrityError)
    assert DatabaseError("private diagnostics").status == 500
    assert DatabaseError("private diagnostics").message == "Database operation failed"


@pytest.mark.asyncio
async def test_users_auth_mapping_and_errors(repository: Database) -> None:
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
async def test_atomic_registration_race_and_refund(repository: Database) -> None:
    db = repository
    await db.add_code("once", "register", gb=4, wl_gb=2)

    async def register(name: str) -> object:
        return await db.register_with_code(
            code="once",
            username=name,
            uuid=name,
            token=name,
            fingerprint="chrome",
            displayname=name,
            ext_username=name,
            ext_password_hash="hash",
        )

    results = await asyncio.gather(register("alice"), register("bob"), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, CodeError) for result in results) == 1
    winner = (await db.list_users())[0]
    assert await db.get_code("once") is None
    await db.rollback_registration_sync(winner, (winner, "once", False))
    assert await db.list_users() == []
    assert (await db.get_code("once")) is not None
    await register("carol")
    await db.confirm_registration_sync("carol")
    assert await db.all_codes() == []


@pytest.mark.asyncio
async def test_registration_rollback_rejects_stale_identity(repository: Database) -> None:
    db = repository
    await db.add_code("invite", "register", uses=2)
    await db.register_with_code(
        code="invite",
        username="alice",
        uuid="first",
        token="first",
        fingerprint="chrome",
        displayname="Alice",
        ext_username="alice",
        ext_password_hash="hash",
    )
    expected = ("first", "invite", False)
    await db.rollback_registration_sync("alice", ("other", "invite", False))
    await db.rollback_registration_sync("alice", ("first", "other", False))
    await db.rollback_registration_sync("alice", ("first", "invite", True))
    assert await db.registration_identity("alice") == expected
    code = await db.get_code("invite")
    assert code is not None
    assert code["uses"] == 1
    await db.confirm_registration_sync("alice")
    await db.set_metadata("uuid_reservation:target", "alice")
    await db.rollback_registration_sync("alice", expected)
    assert await db.user_exists("alice")
    assert await db.get_metadata("uuid_reservation:target") == "alice"
    code = await db.get_code("invite")
    assert code is not None
    assert code["uses"] == 1

    await db.delete_user("alice")
    await db.register_with_code(
        code="invite",
        username="alice",
        uuid="second",
        token="second",
        fingerprint="chrome",
        displayname="Alice",
        ext_username="alice",
        ext_password_hash="hash",
    )
    await db.rollback_registration_sync("alice", expected)
    assert await db.registration_identity("alice") == ("second", "invite", False)
    assert await db.get_code("invite") is None
    assert await db.get_metadata("uuid_reservation:target") == "alice"


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback_second", [False, True])
async def test_shared_code_confirmation_preserves_pending_registration(
    repository: Database,
    *,
    rollback_second: bool,
) -> None:
    db = repository
    await db.add_code("shared", "register", uses=2)

    async def register(name: str) -> None:
        await db.register_with_code(
            code="shared",
            username=name,
            uuid=name,
            token=name,
            fingerprint="chrome",
            displayname=name,
            ext_username=name,
            ext_password_hash="hash",
        )

    await asyncio.gather(register("alice"), register("bob"))
    assert await db.get_code("shared") is None
    await db.confirm_registration_sync("alice")
    assert await db.pending_registrations() == ["bob"]
    assert await db.user_exists("alice")
    async with db.connection() as conn:
        row = await conn.row("SELECT uses FROM codes WHERE code = ?", ("shared",))
        assert row is not None
        assert row["uses"] == 0

    if rollback_second:
        await db.rollback_registration_sync("bob", ("bob", "shared", False))
        restored = await db.get_code("shared")
        assert restored is not None
        assert restored["uses"] == 1
        assert not await db.user_exists("bob")
    else:
        await db.confirm_registration_sync("bob")
        assert await db.user_exists("bob")
        async with db.connection() as conn:
            assert await conn.row("SELECT 1 FROM codes WHERE code = ?", ("shared",)) is None
    assert await db.pending_registrations() == []


@pytest.mark.asyncio
async def test_usage_and_baselines_roll_back_together(repository: Database) -> None:
    await create_user(repository)
    async with repository.transaction(immediate=True) as conn:
        await conn.execute(
            "CREATE TRIGGER reject_baseline BEFORE INSERT ON app_metadata "
            "WHEN NEW.key = 'watcher_usage_baselines' BEGIN SELECT RAISE(ABORT, 'failed'); END",
        )
    with pytest.raises(DatabaseError, match="failed"):
        await repository.increment_usages({"alice": (5, 6)}, baselines="{}")
    user = await repository.get_user("alice")
    assert user is not None
    assert (user["bw_used"], user["wl_used"]) == (0, 0)
    assert await repository.get_metadata("watcher_usage_baselines") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("poll", [False, True])
async def test_monthly_reset_preserves_episodes_and_pending_recovery(
    repository: Database, poll: bool
) -> None:
    await repository.reset_monthly("2026-09", "2026-09-01")
    kept = {
        "alice:expired:1",
        "alice:traffic_exhausted:1",
        "alice:whitelist_exhausted:1",
        "alic:restored",
        "alic:whitelist_restored",
    }
    thresholds = {"alice:expiry_soon:1", "alice:traffic_soon:80", "alic:whitelist_soon:95"}
    await repository.sync_notifications(set(), kept | thresholds)
    intent = (
        '{"uuid":"alice","notices":[{"kind":"EXPIRED"},{"kind":"RESTORED"},'
        '{"kind":"WHITELIST_RESTORED"},{"kind":"TRAFFIC_EXHAUSTED"},'
        '{"kind":"WHITELIST_EXHAUSTED"}]}'
    )
    await repository.set_metadata("user_notifications_pending:alice", intent)
    if poll:
        await repository.increment_usages({}, period=("2026-10", "2026-10-01"))
    else:
        assert await repository.reset_monthly("2026-10", "2026-10-01")
    assert await repository.notification_markers() == kept
    pending = await repository.get_metadata("user_notifications_pending:alice")
    assert pending is not None
    assert '"EXPIRED"' in pending
    assert '"RESTORED"' in pending
    assert '"WHITELIST_RESTORED"' in pending
    assert "EXHAUSTED" not in pending


@pytest.mark.asyncio
async def test_month_rollover_rolls_back_with_baselines(repository: Database) -> None:
    await create_user(repository)
    await repository.reset_monthly("2026-09", "2026-09-01")
    await repository.increment_usages({"alice": (50, 60)}, baselines="old")
    await repository.mark_notification("legacy", 12)
    markers = {"alice:traffic_soon:80", "alice:traffic_exhausted:1"}
    await repository.sync_notifications(set(), markers)
    intent = '{"uuid":"alice","notices":[{"kind":"TRAFFIC_EXHAUSTED"}]}'
    await repository.set_metadata("user_notifications_pending:alice", intent)
    async with repository.transaction(immediate=True) as conn:
        await conn.execute(
            "CREATE TRIGGER reject_rollover BEFORE INSERT ON app_metadata "
            "WHEN NEW.key = 'watcher_usage_baselines' BEGIN SELECT RAISE(ABORT, 'failed'); END",
        )
    with pytest.raises(DatabaseError, match="failed"):
        await repository.increment_usages(
            {"alice": (0, 0)},
            baselines="new",
            period=("2026-10", "2026-10-01"),
        )
    user = await repository.get_user("alice")
    assert user is not None
    assert (user["bw_used"], user["wl_used"]) == (50, 60)
    assert await repository.get_metadata("last_reset_month") == "2026-09"
    assert await repository.get_metadata("watcher_usage_baselines") == "old"
    assert await repository.notification_seen("legacy", 12)
    assert await repository.notification_markers() == markers
    assert await repository.get_metadata("user_notifications_pending:alice") == intent


@pytest.mark.asyncio
async def test_snapshots_and_baselines_roll_back_together(repository: Database) -> None:
    await create_user(repository)
    await repository.add_bandwidth_snapshots([("alice", 10, 1, 2, 3, 4)], baselines="old")
    async with repository.transaction(immediate=True) as conn:
        await conn.execute(
            "CREATE TRIGGER reject_snapshot_baseline BEFORE INSERT ON app_metadata "
            "WHEN NEW.key = 'watcher_snapshot_baselines' BEGIN SELECT RAISE(ABORT, 'failed'); END",
        )
    with pytest.raises(DatabaseError, match="failed"):
        await repository.add_bandwidth_snapshots([("alice", 10, 5, 6, 7, 8)], baselines="new")
    assert await repository.get_bandwidth_snapshots("alice", 0) == [
        {"ts": 10, "up": 1, "down": 2, "wl_up": 3, "wl_down": 4},
    ]
    assert await repository.get_metadata("watcher_snapshot_baselines") == "old"


@pytest.mark.asyncio
async def test_bonus_usage_notifications_and_snapshots(repository: Database) -> None:
    db = repository
    await create_user(db)
    await db.add_code("bonus", "bonus", gb=3, wl_gb=2, days=1)
    results = await asyncio.gather(
        db.adjust_bonus("alice", "bonus"),
        db.adjust_bonus("alice", "bonus"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, CodeError) for result in results) == 1
    user = await db.get_user("alice")
    assert user is not None
    assert user["bw_limit_gb"] == 0
    assert user["wl_limit_gb"] == 0
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
    assert await db.notification_markers() == {"new"}
    await db.set_metadata("prefix_%", "value")
    assert await db.list_metadata("prefix_") == {"prefix_%": "value"}
    await db.delete_metadata("prefix_%")
    await db.set_admin_ui_session("session")
    assert await db.admin_ui_session() == "session"
    await db.set_admin_ui_session(None)
    assert await db.admin_ui_session() is None


@pytest.mark.asyncio
async def test_cancelled_transaction_rolls_back_before_next_writer(
    repository: Database,
) -> None:
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
async def test_cancellation_during_queued_sql_settles(
    repository: Database,
    monkeypatch: MonkeyPatch,
    commit: bool,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    writer = repository._writer
    assert writer is not None
    execute = Connection._execute

    async def gated(self: Connection, sql: str, parameters: Parameters) -> int:
        if sql == ("COMMIT" if commit else "INSERT INTO app_metadata VALUES ('outcome', 'value')"):
            entered.set()
            await release.wait()
        return await execute(self, sql, parameters)

    monkeypatch.setattr(Connection, "_execute", gated)

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
async def test_pragmas_leases_and_nested_rejection(repository: Database) -> None:
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
async def test_backup_restores_and_close_is_repeatable(
    repository: Database,
    tmp_path: Path,
) -> None:
    await create_user(repository)
    await repository.backup_now()
    backup = next((tmp_path / "backups" / "state").glob("*.sqlite3"))
    restored = Database(path=backup, start_backup=False)
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
    await sync.start()
    await sync.set_metadata("existing", "preserved")
    await sync.close()
    db = Database(path=path)
    await db.start()
    assert await db.get_metadata("existing") == "preserved"
    await db.close()
    bad_path = tmp_path / "bad.sqlite3"
    with sqlite3.connect(bad_path) as conn:
        conn.execute("CREATE TABLE unknown (id INTEGER)")
    bad = Database(path=bad_path)
    with pytest.raises(MigrationError):
        await bad.start()
    assert bad._writer is None
    assert not bad._readers
    await bad.close()


@pytest.mark.asyncio
async def test_rollback_failure_discards_writer(
    repository: Database,
    monkeypatch: MonkeyPatch,
) -> None:
    original = Connection._execute
    old_writer = repository._writer

    async def broken(self: Connection, sql: str, parameters: Parameters) -> int:
        if sql == "ROLLBACK":
            raise sqlite3.OperationalError("injected rollback failure")
        return await original(self, sql, parameters)

    monkeypatch.setattr(Connection, "_execute", broken)

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
async def test_readers_do_not_see_uncommitted_writer_and_close_drains(
    repository: Database,
) -> None:
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
    restored = Database(path=repository.path)
    await restored.start()
    try:
        assert await restored.get_metadata("pending") == "value"
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_code_duplicates_and_failed_registration_preserve_code(
    repository: Database,
) -> None:
    await create_user(repository)
    await repository.add_code("register", "register")
    with pytest.raises(DuplicateError):
        await repository.add_code("register", "register")
    with pytest.raises(DuplicateError):
        await repository.register_with_code(
            code="register",
            username="alice",
            uuid="new",
            token="new",
            fingerprint="chrome",
            displayname="Alice",
            ext_username="new",
            ext_password_hash="hash",
        )
    code = await repository.get_code("register")
    assert code is not None
    assert code["uses"] == 1
    await repository.add_code("permanent", "bonus", permanent=True)
    assert (await repository.consume_code("permanent", "bonus"))["uses"] == -1
    assert await repository.delete_code("permanent")
    assert not await repository.delete_code("permanent")
