from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from threadutil import named_to_thread

from .tasks import AdmissionClosedError, TaskRegistry

if TYPE_CHECKING:
    from collections.abc import Callable


class BoundedOffload:
    """Bound submissions, retaining ownership until the thread actually finishes.

    Excess callers wait asynchronously before submitting work. Cancellation
    while waiting does not submit work; after submission it only ends the
    caller's wait. A permit is not released until
    the synchronous operation has completed, and close drains before dependents
    may be released. The loop's executor is used; no executor starts on import.
    """

    def __init__(self, *, capacity: int, name: str) -> None:
        self._tasks = TaskRegistry(capacity=capacity)
        self._capacity = capacity
        self._available = asyncio.Event()
        self._accepting = True
        self._name = name

    @property
    def active_count(self) -> int:
        return self._tasks.active_count

    def stop_admission(self) -> None:
        self._accepting = False
        self._tasks.stop_admission()
        self._available.set()

    async def run[**P, T](self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        await asyncio.sleep(0)
        while self._accepting and self.active_count >= self._capacity:
            self._available.clear()
            await self._available.wait()
        if not self._accepting:
            raise AdmissionClosedError("offload admission is closed")

        caller = asyncio.current_task()
        operation_name = caller.get_name() if caller is not None else ""
        if not operation_name or operation_name.startswith("Task-"):
            operation_name = getattr(function, "__qualname__", type(function).__name__)

        async def operation() -> T:
            return await named_to_thread(
                f"{self._name}:{operation_name}", function, *args, **kwargs,
            )

        task = self._tasks.create(operation, name=f"offload:{self._name}")
        # The registry removes the completed task before waking waiting callers.
        task.add_done_callback(lambda _: self._available.set())
        return await asyncio.shield(task)

    async def close(self) -> None:
        self.stop_admission()
        await self._tasks.drain()


@dataclass(frozen=True, slots=True)
class OffloadServices:
    files: BoundedOffload = field(default_factory=lambda: BoundedOffload(capacity=6, name="files"))
    passwords: BoundedOffload = field(
        default_factory=lambda: BoundedOffload(capacity=2, name="passwords"),
    )
    rendering: BoundedOffload = field(
        default_factory=lambda: BoundedOffload(capacity=2, name="rendering"),
    )
    metrics: BoundedOffload = field(
        default_factory=lambda: BoundedOffload(capacity=2, name="metrics"),
    )

    def stop_admission(self) -> None:
        for service in (self.files, self.passwords, self.rendering, self.metrics):
            service.stop_admission()

    async def close(self) -> None:
        await asyncio.gather(
            *(
                service.close()
                for service in (
                    self.files,
                    self.passwords,
                    self.rendering,
                    self.metrics,
                )
            ),
        )
