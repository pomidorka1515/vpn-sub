from __future__ import annotations

import asyncio
import copy
from time import monotonic
from typing import TYPE_CHECKING

from .stamp import bump_stamp, read_stamp

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from app_runtime.offload import BoundedOffload
    from app_runtime.tasks import TaskRegistry


class AsyncGenerationCache[T]:
    """Single-flight detached values with pre-fetch generation publication."""

    def __init__(
        self,
        path: str,
        *,
        files: BoundedOffload,
        tasks: TaskRegistry,
        disk_lock: asyncio.Lock | None = None,
    ) -> None:
        self.path = path
        self._files = files
        self._tasks = tasks
        self._lock = asyncio.Lock()
        self._disk_lock = disk_lock or asyncio.Lock()
        self._epoch = 0
        self._value: T | None = None
        self._stamp: int | None = None
        self._stored_at = 0.0
        self._flight: tuple[int, int, asyncio.Task[T]] | None = None

    async def _read_stamp(self) -> int:
        async with self._disk_lock:
            return await self._files.run(read_stamp, self.path)

    async def get(self, ttl: float, load: Callable[[], Coroutine[object, object, T]]) -> T:
        async with self._lock:
            stamp = await self._read_stamp()
            if (
                stamp == self._stamp
                and self._value is not None
                and monotonic() - self._stored_at < ttl
            ):
                return copy.deepcopy(self._value)
            epoch = self._epoch
            if self._flight is None or self._flight[:2] != (epoch, stamp) or self._flight[2].done():
                task = self._tasks.create(
                    lambda: self._refresh(epoch, stamp, load), name="panel:cache-refresh"
                )
                self._flight = (epoch, stamp, task)
            else:
                task = self._flight[2]
        return copy.deepcopy(await asyncio.shield(task))

    async def _refresh(
        self, epoch: int, stamp: int, load: Callable[[], Coroutine[object, object, T]]
    ) -> T:
        value = await load()
        async with self._lock:
            current = await self._read_stamp()
            if epoch == self._epoch and stamp == current:
                self._value = copy.deepcopy(value)
                self._stamp = stamp
                self._stored_at = monotonic()
        return value

    async def clear(self) -> None:
        async with self._lock:
            self._epoch += 1
            self._value = None
            self._stamp = None
            async with self._disk_lock:
                await self._files.run(bump_stamp, self.path)
