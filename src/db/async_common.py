from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Coroutine, Iterable
    from contextlib import AbstractAsyncContextManager

    import aiosqlite

type Parameters = Iterable[object]


async def settle[T](operation: Coroutine[object, object, T]) -> T:
    """Finish queued worker work before propagating even repeated cancellation."""
    task = asyncio.create_task(operation, name="database:sql")
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        # Observe any worker error, but preserve the caller's cancellation.
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


class AsyncConnection:
    """Buffered SQL helpers: no live cursor escapes a repository operation."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self.connection = connection

    async def execute(self, sql: str, parameters: Parameters = ()) -> int:
        return await settle(self._execute(sql, parameters))

    async def _execute(self, sql: str, parameters: Parameters) -> int:
        async with self.connection.execute(sql, parameters) as cursor:
            return cursor.rowcount

    async def executemany(self, sql: str, parameters: Iterable[Parameters]) -> None:
        await settle(self._executemany(sql, parameters))

    async def _executemany(self, sql: str, parameters: Iterable[Parameters]) -> None:
        async with self.connection.executemany(sql, parameters):
            pass

    async def rows(self, sql: str, parameters: Parameters = ()) -> list[sqlite3.Row]:
        return await settle(self._rows(sql, parameters))

    async def _rows(self, sql: str, parameters: Parameters) -> list[sqlite3.Row]:
        async with self.connection.execute(sql, parameters) as cursor:
            return list(await cursor.fetchall())

    async def row(self, sql: str, parameters: Parameters = ()) -> sqlite3.Row | None:
        return await settle(self._row(sql, parameters))

    async def _row(self, sql: str, parameters: Parameters) -> sqlite3.Row | None:
        async with self.connection.execute(sql, parameters) as cursor:
            return await cursor.fetchone()


class AsyncConnectionMixin:
    def connection(self) -> AbstractAsyncContextManager[AsyncConnection]:
        raise NotImplementedError

    def transaction(
        self, *, immediate: bool = False,
    ) -> AbstractAsyncContextManager[AsyncConnection]:
        raise NotImplementedError
