from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class AsyncCloseable(Protocol):
    async def close(self) -> None: ...


class AsyncStartable(Protocol):
    async def start(self) -> None: ...


class AsyncResource(AsyncStartable, AsyncCloseable, Protocol):
    """Constructors assemble dependencies; start opens them, close releases them.

    Close must also handle a partially failed start. Resources are registered in
    dependency order, with the primary lock first so it is released last.
    """


@dataclass(frozen=True, slots=True)
class OwnedResource:
    name: str
    resource: AsyncResource
