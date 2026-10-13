from __future__ import annotations

import asyncio
from enum import Enum, auto
from typing import TYPE_CHECKING

from .offload import OffloadServices
from .tasks import AdmissionClosedError, MutationRegistry, TaskRegistry

if TYPE_CHECKING:
    from .interfaces import OwnedResource


class RuntimeState(Enum):
    NEW = auto()
    STARTING = auto()
    READY = auto()
    STOPPING = auto()
    STOPPED = auto()


class Runtime[DependenciesT]:
    """Inject a typed dependency bundle and explicitly ordered resource lifetimes.

    This infrastructure imports no production application. DependenciesT is the
    caller's concrete composition dataclass (config/repository/Redis/panels/core/
    watcher/bots/writers/primary lock), preserving its field types for routes.
    Constructors must not open resources or launch background work.
    """

    def __init__(
        self,
        dependencies: DependenciesT,
        *,
        resources: tuple[OwnedResource, ...] = (),
        offloads: OffloadServices | None = None,
        tasks: TaskRegistry | None = None,
        mutations: MutationRegistry | None = None,
    ) -> None:
        self.dependencies = dependencies
        self.offloads = offloads if offloads is not None else OffloadServices()
        self.tasks = tasks if tasks is not None else TaskRegistry()
        self.mutations = mutations if mutations is not None else MutationRegistry()
        self.state = RuntimeState.NEW
        self.stopping = asyncio.Event()
        self._resources = resources
        self._opened: list[OwnedResource] = []
        self._lifecycle_lock = asyncio.Lock()
        self._stop_task: asyncio.Task[None] | None = None

    @property
    def ready(self) -> bool:
        return self.state is RuntimeState.READY and not self.stopping.is_set()

    def require_ready(self) -> DependenciesT:
        if not self.ready:
            raise AdmissionClosedError("runtime is not ready")
        return self.dependencies

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self.ready:
                return
            if self.state is not RuntimeState.NEW or self.stopping.is_set():
                raise AdmissionClosedError("runtime cannot restart")
            self.state = RuntimeState.STARTING
            try:
                for resource in self._resources:
                    # Register before starting to unwind partially opened clients.
                    self._opened.append(resource)
                    await resource.resource.start()
                self.state = RuntimeState.READY
            except Exception, asyncio.CancelledError:
                self._begin_stop()
                # Retain cleanup ownership even if startup is cancelled again.
                if self._stop_task is None:
                    self._stop_task = asyncio.create_task(
                        self._shutdown(),
                        name="runtime:startup-unwind",
                    )
                    await asyncio.shield(self._stop_task)
                raise

    def _begin_stop(self) -> None:
        self.stopping.set()
        self.state = RuntimeState.STOPPING
        self.tasks.stop_admission()
        self.mutations.stop_admission()
        # Offload admission remains available to already admitted mutations and
        # their compensation until they have settled.

    async def stop(self) -> None:
        if self._stop_task is None:
            self._begin_stop()
            self._stop_task = asyncio.create_task(self._stop(), name="runtime:stop")
        await asyncio.shield(self._stop_task)

    async def _stop(self) -> None:
        async with self._lifecycle_lock:
            await self._shutdown()

    async def _shutdown(self) -> None:
        errors: list[Exception] = []
        # Stop producers/settle their handlers before draining mutations, while
        # transports, audit writers and offload services are still available.
        for resource in reversed(self._opened):
            if resource.quiesce is not None:
                try:
                    await resource.quiesce()
                except Exception as error:
                    error.add_note(f"quiescing runtime resource: {resource.name}")
                    errors.append(error)
        await self.tasks.drain(cancel=True)
        await self.mutations.drain()
        while self._opened:
            resource = self._opened.pop()
            try:
                await resource.resource.close()
            except Exception as error:
                error.add_note(f"closing runtime resource: {resource.name}")
                errors.append(error)
        self.offloads.stop_admission()
        await self.offloads.close()
        self.state = RuntimeState.STOPPED
        if errors:
            raise ExceptionGroup("runtime cleanup failed", errors)
