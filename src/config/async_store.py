from __future__ import annotations

import asyncio
import copy
import logging
from typing import TYPE_CHECKING, cast

from app_runtime.tasks import AdmissionClosedError, TaskRegistry

from .constants import JsonDict

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping

    from app_runtime.offload import BoundedOffload

    from .constants import JsonValue
    from .core import Config
    from .lines import LinesConfig

log = logging.getLogger(__name__)


class FileSchedules:
    """Owned, non-overlapping delay-after-completion file jobs."""

    def __init__(self) -> None:
        self.tasks = TaskRegistry()

    def add(self, operation: Callable[[], Awaitable[object]], *, interval: float, name: str) -> None:
        if interval <= 0:
            raise ValueError("file schedule interval must be positive")

        async def loop() -> None:
            while True:
                await asyncio.sleep(interval)
                try:
                    await operation()
                except Exception:
                    log.exception("scheduled file job %s failed", name)

        self.tasks.create(loop, name=name)

    async def close(self) -> None:
        await self.tasks.drain(cancel=True)


class AsyncConfig[Doc = JsonDict]:
    """Detached memory reads; complete locked file operations run off-loop.

    The factory must disable Config's backup thread (start_backup=False).
    Once admitted, operations finish and publish even if their waiter cancels.
    """

    def __init__(
        self, factory: Callable[[], Config[Doc]], *, files: BoundedOffload,
        reload_interval: float = 5,
    ) -> None:
        if reload_interval <= 0:
            raise ValueError("reload_interval must be positive")
        self._factory = factory
        self._files = files
        self._reload_interval = reload_interval
        self._store: Config[Doc] | None = None
        self._snapshot: Doc | None = None
        self._lock = asyncio.Lock()
        self._operations = TaskRegistry(capacity=32)
        self._schedules = FileSchedules()
        self._closing = False
        self._start_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._closing:
            raise AdmissionClosedError("config is closing")
        if self._start_task is None:
            self._start_task = asyncio.create_task(self._start(), name="config:start")
        await asyncio.shield(self._start_task)

    async def _start(self) -> None:
        async with self._lock:
            if self._closing:
                raise AdmissionClosedError("config is closing")
            if self._store is not None:
                return
            self._store = await self._files.run(self._factory)
            self._snapshot = await self._files.run(self._store.view)
            self._schedules.add(self.reload, interval=self._reload_interval, name="config:reload")
            if self._store.backup_dir is not None:
                self._schedules.add(self.backup_now, interval=self._store.backup_interval, name="config:backup")

    def view(self) -> Doc:
        if self._snapshot is None:
            raise RuntimeError("config has not started")
        return copy.deepcopy(self._snapshot)

    def _require_store(self) -> Config[Doc]:
        if self._store is None:
            raise RuntimeError("config has not started")
        return self._store

    async def reload(self) -> bool:
        async def operation() -> bool:
            async with self._lock:
                store = self._require_store()

                def load() -> tuple[bool, Doc]:
                    changed = store.reload()
                    return changed, store.view()

                changed, snapshot = await self._files.run(load)
                self._snapshot = snapshot
                return changed

        return await asyncio.shield(self._operations.create(operation, name="config:load"))

    async def mutate[T](self, callback: Callable[[MutableMapping[str, JsonValue]], T]) -> T:
        async def operation() -> T:
            async with self._lock:
                store = self._require_store()

                def commit() -> tuple[T, Doc]:
                    result = store.mutate(callback)
                    # Copy committed memory without another freshness check:
                    # publication must describe this successful commit.
                    with store.lock:
                        return result, copy.deepcopy(cast(Doc, store.data))

                result, snapshot = await self._files.run(commit)
                self._snapshot = snapshot
                return result

        return await asyncio.shield(self._operations.create(operation, name="config:commit"))

    async def backup_now(self) -> None:
        async def operation() -> None:
            async with self._lock:
                await self._files.run(self._require_store().backup_now)

        await asyncio.shield(self._operations.create(operation, name="config:backup-now"))

    async def close(self) -> None:
        if self._close_task is None:
            self._closing = True
            self._operations.stop_admission()
            self._close_task = asyncio.create_task(self._close(), name="config:close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        if self._start_task is not None:
            await asyncio.gather(self._start_task, return_exceptions=True)
        await self._schedules.close()
        await self._operations.drain()
        async with self._lock:
            if self._store is not None:
                await self._files.run(self._store.close)


class AsyncLinesConfig:
    """Acknowledged durable append boundary, separate from best-effort logs."""

    def __init__(self, factory: Callable[[], LinesConfig], *, files: BoundedOffload) -> None:
        self._factory = factory
        self._files = files
        self._store: LinesConfig | None = None
        self._operations = TaskRegistry(capacity=32)
        self._schedules = FileSchedules()
        self._lock = asyncio.Lock()
        self._start_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._close_task is not None:
            raise AdmissionClosedError("lines store is closing")
        if self._start_task is None:
            self._start_task = asyncio.create_task(self._start(), name="lines:start")
        await asyncio.shield(self._start_task)

    async def _start(self) -> None:
        async with self._lock:
            if self._close_task is not None:
                raise AdmissionClosedError("lines store is closing")
            if self._store is None:
                self._store = await self._files.run(self._factory)
                if self._store.backup_dir is not None:
                    self._schedules.add(self.backup_now, interval=self._store.backup_interval, name="lines:backup")

    def _require_store(self) -> LinesConfig:
        if self._store is None:
            raise RuntimeError("lines store has not started")
        return self._store

    async def append(self, record: JsonDict) -> None:
        detached = copy.deepcopy(record)

        async def operation() -> None:
            async with self._lock:
                await self._files.run(self._require_store().append, detached)

        await asyncio.shield(self._operations.create(operation, name="lines:append"))

    async def backup_now(self) -> None:
        async def operation() -> None:
            async with self._lock:
                await self._files.run(self._require_store().backup_now)

        await asyncio.shield(self._operations.create(operation, name="lines:backup-now"))

    async def read_all(self) -> list[JsonDict]:
        return await self._files.run(self._require_store().read_all)

    async def tail(self, count: int) -> list[JsonDict]:
        return await self._files.run(lambda: list(self._require_store().tail(count)))

    async def close(self) -> None:
        if self._close_task is None:
            self._operations.stop_admission()
            self._close_task = asyncio.create_task(self._close(), name="lines:close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        if self._start_task is not None:
            await asyncio.gather(self._start_task, return_exceptions=True)
        await self._schedules.close()
        await self._operations.drain()
        async with self._lock:
            if self._store is not None:
                await self._files.run(self._store.close)
