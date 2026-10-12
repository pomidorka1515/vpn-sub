from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

log = logging.getLogger(__name__)


class AdmissionClosedError(RuntimeError):
    """The runtime is draining and cannot accept work."""


class CapacityExceededError(RuntimeError):
    """Bounded admission is full; no work has been submitted."""


class TaskRegistry:
    """Own named tasks and observe failures, including detached caller work.

    Factories are called only after admission. Capacity rejects excess work
    rather than accumulating an unlimited queue of tasks waiting on a semaphore.
    """

    def __init__(self, *, capacity: int | None = None) -> None:
        if capacity is not None and capacity < 1:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._tasks: set[asyncio.Task[object]] = set()
        self._accepting = True

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    def stop_admission(self) -> None:
        self._accepting = False

    def create[T](
        self, factory: Callable[[], Coroutine[object, object, T]], *, name: str,
    ) -> asyncio.Task[T]:
        if not self._accepting:
            raise AdmissionClosedError("task admission is closed")
        if self._capacity is not None and len(self._tasks) >= self._capacity:
            raise CapacityExceededError("task capacity exceeded")
        if not name:
            raise ValueError("owned tasks require a name")
        task = asyncio.create_task(factory(), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._completed)
        return task

    def _completed(self, task: asyncio.Task[object]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            # Retrieve exceptions even if the original caller has disconnected.
            log.error("owned task %s failed", task.get_name(), exc_info=error)

    async def drain(self, *, cancel: bool = False) -> None:
        """Stop admission and settle all work, optionally requesting cancellation.

        No timeout implies successful cleanup: callers own deadlines and must
        leave resources open if settlement cannot finish.
        """
        self.stop_admission()
        tasks = tuple(self._tasks)
        if cancel:
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class MutationRegistry(TaskRegistry):
    """Critical operations survive cancellation of their waiting caller."""

    def __init__(self, *, capacity: int = 32) -> None:
        super().__init__(capacity=capacity)

    async def run[T](
        self, factory: Callable[[], Coroutine[object, object, T]], *, name: str,
    ) -> T:
        # Deliver cancellation before admission; after this point ownership is
        # transferred to the registry, not the HTTP/bot/watcher caller.
        await asyncio.sleep(0)
        task = self.create(factory, name=name)
        return await asyncio.shield(task)
