from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from app_runtime.offload import BoundedOffload
from errors import DatabaseError
from loggers import Logger

from .async_codes import AsyncCodesMixin
from .async_common import AsyncConnection, settle
from .async_schema import AsyncSchemaMixin
from .async_state import AsyncStateMixin
from .async_telegram import AsyncTelegramMixin
from .async_users import AsyncUsersMixin
from .backup import do_backup, instance_backup_dir, prune_backups

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class AsyncDatabase(
    AsyncUsersMixin, AsyncCodesMixin, AsyncTelegramMixin, AsyncStateMixin, AsyncSchemaMixin,
):
    """Single-loop repository with a transaction-owned writer and leased readers."""

    def __init__(
        self,
        *,
        path: str | Path,
        timeout: float = 5.0,
        read_connections: int = 2,
        backup_dir: str | Path | None = None,
        backup_interval: float = 7200,
        backup_retention: int = 3,
        start_backup: bool = True,
    ) -> None:
        if read_connections < 1 or backup_interval <= 0 or backup_retention < 1:
            raise ValueError("connection count, backup interval and retention must be positive")
        self.path = os.path.normpath(Path(path).absolute())
        self.timeout = timeout
        self.log = Logger(type(self).__name__)
        self.backup_dir = str(backup_dir) if backup_dir else None
        self.backup_interval = backup_interval
        self.backup_retention = backup_retention
        self._start_backup = start_backup
        self._read_count = read_connections
        self._writer: aiosqlite.Connection | None = None
        self._readers: list[aiosqlite.Connection] = []
        self._write_lock = asyncio.Lock()
        self._read_locks = [asyncio.Lock() for _ in range(read_connections)]
        self._read_slots: asyncio.Queue[int] = asyncio.Queue()
        self._owners: set[asyncio.Task[object]] = set()
        self._files = BoundedOffload(capacity=1, name="database-files")
        self._backup_lock = asyncio.Lock()
        self._backup_task: asyncio.Task[None] | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closing = False
        self._ready = False

    async def start(self) -> None:
        if self._closing:
            raise DatabaseError("database is closed")
        if self._start_task is None:
            self._start_task = asyncio.create_task(self._start(), name="database:start")
        await settle(self._wait_start())

    async def _wait_start(self) -> None:
        if self._start_task is not None:
            await asyncio.shield(self._start_task)

    async def _open(self, *, reader: bool = False) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self.path, timeout=self.timeout, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            wrapped = AsyncConnection(conn)
            for statement in (
                "PRAGMA foreign_keys = ON",
                f"PRAGMA busy_timeout = {max(1, int(self.timeout * 1000))}",
                "PRAGMA journal_mode = WAL",
                "PRAGMA synchronous = NORMAL",
            ):
                await wrapped.execute(statement)
            if reader:
                await wrapped.execute("PRAGMA query_only = ON")
            return conn
        except BaseException:
            await settle(conn.close())
            raise

    async def _start(self) -> None:
        try:
            await self._files.run(Path(self.path).parent.mkdir, parents=True, exist_ok=True)
            self._writer = await self._open()
            # Initialization owns the writer before admission is published.
            self._ready = True
            await self.initialize()
            for index in range(self._read_count):
                self._readers.append(await self._open(reader=True))
                self._read_slots.put_nowait(index)
            if self.backup_dir and self._start_backup and not self._closing:
                self._backup_task = asyncio.create_task(self._backup_loop(), name="database:backup")
        except BaseException as exc:
            self._ready = False
            await self._close_connections()
            await self._files.close()
            if isinstance(exc, (sqlite3.Error, OSError)):
                raise DatabaseError(str(exc)) from exc
            raise

    def _check(self) -> asyncio.Task[object]:
        task = asyncio.current_task()
        if not self._ready or self._closing or task is None:
            raise DatabaseError("database is not accepting operations")
        if (
            self._start_task is not None
            and not self._start_task.done()
            and task is not self._start_task
        ):
            raise DatabaseError("database startup is not complete")
        if task in self._owners:
            raise DatabaseError(
                "nested repository leases are not supported; reuse the leased connection",
            )
        return task

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[AsyncConnection]:
        task = self._check()
        index = await self._read_slots.get()
        try:
            async with self._read_locks[index]:
                self._check()
                self._owners.add(task)
                conn = self._readers[index]
                try:
                    yield AsyncConnection(conn)
                except sqlite3.Error as exc:
                    await settle(self._replace_reader(index))
                    raise DatabaseError(str(exc)) from exc
                finally:
                    self._owners.discard(task)
        finally:
            self._read_slots.put_nowait(index)

    async def _replace_reader(self, index: int) -> None:
        await settle(self._readers[index].close())
        try:
            self._readers[index] = await settle(self._open(reader=True))
        except Exception:
            self._ready = False
            raise

    @asynccontextmanager
    async def transaction(self, *, immediate: bool = False) -> AsyncGenerator[AsyncConnection]:
        task = self._check()
        async with self._write_lock:
            self._check()
            self._owners.add(task)
            try:
                if self._writer is None:
                    await settle(self._open_writer())
                conn = self._writer
                if conn is None:
                    raise DatabaseError("database writer is unavailable")
                wrapped = AsyncConnection(conn)
                try:
                    await wrapped.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                    yield wrapped
                    # Cancellation waits for COMMIT's actual outcome. A committed
                    # operation is never retried automatically by this repository.
                    await wrapped.execute("COMMIT")
                except BaseException as exc:
                    await settle(self._recover_writer(conn, discard=isinstance(exc, sqlite3.Error)))
                    if isinstance(exc, sqlite3.Error):
                        raise DatabaseError(str(exc)) from exc
                    raise
            finally:
                self._owners.discard(task)

    async def _open_writer(self) -> None:
        # Publish ownership inside the settled task, even if its waiter cancels.
        self._writer = await self._open()

    async def _recover_writer(self, conn: aiosqlite.Connection, *, discard: bool = False) -> None:
        try:
            if conn.in_transaction:
                await AsyncConnection(conn).execute("ROLLBACK")
            if conn.in_transaction:
                raise DatabaseError("rollback left an active transaction")
            if discard:
                self._writer = None
                await conn.close()
        except Exception:
            self._writer = None
            await conn.close()

    async def backup_now(self) -> None:
        self._check()
        async with self._backup_lock:
            self._check()
            if self.backup_dir is None:
                raise DatabaseError("backup_now() requires a backup_dir to be configured.")
            await self._files.run(self._backup)

    def _backup(self) -> None:
        if self.backup_dir is None:
            raise DatabaseError("backup directory is not configured")
        directory = instance_backup_dir(self.path, self.backup_dir)
        do_backup(self.path, self.timeout, directory, self.log)
        prune_backups(directory, self.backup_retention, self.log)

    async def _backup_loop(self) -> None:
        failures = 0
        delay = self.backup_interval
        while True:
            await asyncio.sleep(delay)
            try:
                await self.backup_now()
                failures = 0
                delay = self.backup_interval
            except Exception:
                failures += 1
                delay = min(self.backup_interval, 60 * failures)
                if failures == 1 or failures % 3 == 0:
                    self.log.exception("database backup failed (%s attempts)", failures)

    async def close(self) -> None:
        if asyncio.current_task() in self._owners:
            raise DatabaseError("cannot close database from an active lease")
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._close(), name="database:close")
        await settle(self._wait_close())

    async def _wait_close(self) -> None:
        if self._close_task is not None:
            await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        if self._start_task is not None:
            await asyncio.gather(self._start_task, return_exceptions=True)
        if self._backup_task is not None:
            self._backup_task.cancel()
            await asyncio.gather(self._backup_task, return_exceptions=True)
        async with self._backup_lock:
            await self._files.close()
        async with self._write_lock:
            for lock in self._read_locks:
                await lock.acquire()
            try:
                await self._close_connections()
                self._ready = False
            finally:
                for lock in self._read_locks:
                    lock.release()

    async def _close_connections(self) -> None:
        connections = [*self._readers]
        if self._writer is not None:
            connections.append(self._writer)
        self._readers.clear()
        self._writer = None
        for conn in connections:
            await conn.close()
