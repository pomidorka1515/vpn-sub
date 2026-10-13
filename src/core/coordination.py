from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from functools import wraps
from inspect import signature
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence

    from app_runtime.tasks import MutationRegistry


@dataclass(slots=True)
class _Entry:
    lock: asyncio.Lock
    borrowers: int = 0


class MutationCoordinator:
    """Bound critical work, order user locks, and release unused lock entries."""

    def __init__(self, registry: MutationRegistry) -> None:
        self.registry = registry
        self._entries: dict[str, _Entry] = {}
        self._owners: dict[asyncio.Task[object], frozenset[str]] = {}

    @property
    def lock_count(self) -> int:
        return len(self._entries)

    async def run[T](
        self,
        users: Sequence[str],
        operation: Callable[[], Coroutine[object, object, T]],
        *,
        name: str,
    ) -> T:
        keys = frozenset(users)
        task = asyncio.current_task()
        held = self._owners.get(task) if task is not None else None
        if held is not None:
            if not keys <= held:
                raise RuntimeError("nested mutations must not expand their user lock set")
            return await operation()

        async def coordinated() -> T:
            owner = asyncio.current_task()
            if owner is None:
                raise RuntimeError("mutation requires a task")
            entries = [
                (key, self._entries.setdefault(key, _Entry(asyncio.Lock()))) for key in sorted(keys)
            ]
            for _, entry in entries:
                entry.borrowers += 1
            try:
                async with AsyncExitStack() as stack:
                    for _, entry in entries:
                        await stack.enter_async_context(entry.lock)
                    self._owners[owner] = keys
                    try:
                        return await operation()
                    finally:
                        del self._owners[owner]
            finally:
                for key, entry in entries:
                    entry.borrowers -= 1
                    if entry.borrowers == 0:
                        del self._entries[key]

        async def owned() -> T:
            # Even forced registry cancellation must settle the critical child
            # before releasing locks or allowing dependency shutdown.
            child = asyncio.create_task(coordinated(), name=f"{name}:settlement")
            cancelled = False
            while not child.done():
                try:
                    await asyncio.shield(child)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break
            if cancelled:
                if not child.cancelled():
                    child.exception()
                raise asyncio.CancelledError
            return child.result()

        return await self.registry.run(owned, name=name)


def user_mutation[**P, T](
    function: Callable[P, Coroutine[object, object, T]],
) -> Callable[P, Coroutine[object, object, T]]:
    """Nested calls reuse ownership only in the actual owning task."""
    sig = signature(function)

    @wraps(function)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        from .common import BaseService

        bound = sig.bind(*args, **kwargs)
        service = bound.arguments["self"]
        username = bound.arguments["username"]
        if not isinstance(service, BaseService) or not isinstance(username, str):
            raise TypeError("user mutation requires a service and username")
        return await service.res.coordinator.run(
            [username], lambda: function(*args, **kwargs), name=f"core:{function.__name__}",
        )

    return wrapped
