from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from db import Database
from db.backup import instance_backup_dir
from db.common import Connection
from errors import CodeError, DatabaseError, MigrationError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

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


def _database_backup(instance: Path) -> Path:
    return next(instance.glob("*.sqlite3"))


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_mode", ["normal", "exception", "os_error", "cancel"])
async def test_reader_lease_clears_snapshot_on_exit(tmp_path: Path, exit_mode: str) -> None:
    db = Database(path=tmp_path / "reader.sqlite3", read_connections=1, start_backup=False)
    await db.start()
    entered, release = asyncio.Event(), asyncio.Event()

    async def borrow() -> None:
        async with db.connection() as conn:
            await conn.execute("BEGIN")
            assert await conn.row("SELECT value FROM app_metadata WHERE key = 'snapshot'") is None
            entered.set()
            await release.wait()
            if exit_mode == "exception":
                raise ValueError("borrower failed") from None
            if exit_mode == "os_error":
                raise OSError("borrower failed") from None

    try:
        original = db._readers[0]
        task = asyncio.create_task(borrow())
        await entered.wait()
        await db.set_metadata("snapshot", "fresh")
        if exit_mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            if exit_mode == "exception":
                with pytest.raises(ValueError, match="borrower failed"):
                    await task
            elif exit_mode == "os_error":
                with pytest.raises(OSError, match="borrower failed"):
                    await task
            else:
                await task
        assert not original.in_transaction
        assert db._readers[0] is original
        assert await db.get_metadata("snapshot") == "fresh"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reader_rollback_failure_replaces_connection(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    db = Database(path=tmp_path / "reader.sqlite3", read_connections=1, start_backup=False)
    await db.start()
    original_execute = Connection._execute
    old_reader = db._readers[0]

    async def broken(self: Connection, sql: str, parameters: Parameters) -> int:
        if sql == "ROLLBACK":
            raise sqlite3.OperationalError("injected rollback failure") from None
        return await original_execute(self, sql, parameters)

    monkeypatch.setattr(Connection, "_execute", broken)
    try:
        async with db.connection() as conn:
            await conn.execute("BEGIN")
            await conn.row("SELECT * FROM app_metadata")
        assert db._readers[0] is not old_reader
        assert db._read_slots.qsize() == 1
        await db.set_metadata("later", "fresh")
        assert await db.get_metadata("later") == "fresh"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reader_replacement_failure_stops_admission(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    db = Database(path=tmp_path / "reader.sqlite3", read_connections=1, start_backup=False)
    await db.start()
    original_execute = Connection._execute

    async def broken(self: Connection, sql: str, parameters: Parameters) -> int:
        if sql == "ROLLBACK":
            raise sqlite3.OperationalError("injected rollback failure") from None
        return await original_execute(self, sql, parameters)

    async def fail_open(*, reader: bool = False) -> None:
        raise sqlite3.OperationalError("injected reopen failure") from None

    monkeypatch.setattr(Connection, "_execute", broken)
    monkeypatch.setattr(db, "_open", fail_open)

    async def borrow() -> None:
        async with db.connection() as conn:
            await conn.execute("BEGIN")
            await conn.row("SELECT * FROM app_metadata")

    try:
        with pytest.raises(DatabaseError, match="injected reopen failure"):
            await borrow()
        with pytest.raises(DatabaseError, match="not accepting"):
            async with db.connection():
                pass
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cancelled_reader_cleanup_finishes_before_shutdown(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    db = Database(path=tmp_path / "reader.sqlite3", read_connections=1, start_backup=False)
    await db.start()
    entered, release = asyncio.Event(), asyncio.Event()
    original_execute = Connection._execute

    async def gated(self: Connection, sql: str, parameters: Parameters) -> int:
        if sql == "ROLLBACK":
            entered.set()
            await release.wait()
        return await original_execute(self, sql, parameters)

    monkeypatch.setattr(Connection, "_execute", gated)

    async def borrow() -> None:
        async with db.connection() as conn:
            await conn.execute("BEGIN")
            await conn.row("SELECT * FROM app_metadata")

    task = asyncio.create_task(borrow())
    try:
        await entered.wait()
        task.cancel()
        task.cancel()
        closing = asyncio.create_task(db.close())
        await asyncio.sleep(0)
        assert not task.done()
        assert not closing.done()
        assert db._read_slots.empty()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await closing
        assert not db._readers
    finally:
        release.set()
        await db.close()


@pytest.mark.asyncio
async def test_pragmas_leases_and_nested_rejection(repository: Database) -> None:
    async with repository.connection() as conn:
        assert (await conn.row("PRAGMA foreign_keys"))[0] == 1  # type: ignore[index]
        assert (await conn.row("PRAGMA journal_mode"))[0] == "wal"  # type: ignore[index]
        assert (await conn.row("PRAGMA cache_size"))[0] == -65536  # type: ignore[index]
        assert (await conn.row("PRAGMA mmap_size"))[0] == 268435456  # type: ignore[index]
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
    backup_dir = tmp_path / "backups"
    instance = Path(instance_backup_dir(repository.path, str(backup_dir)))
    backup = _database_backup(instance)
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
            raise sqlite3.OperationalError("injected rollback failure") from None
        return await original(self, sql, parameters)

    monkeypatch.setattr(Connection, "_execute", broken)

    async def operation() -> None:
        async with repository.transaction(immediate=True) as conn:
            await conn.execute("INSERT INTO app_metadata VALUES ('failed', 'value')")
            raise CodeError("abort") from None

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
