from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from custom_types import JsonifyValue


class LiveState:
    """One demand-driven sampler, with bounded latest-value queues per viewer."""

    def __init__(self, sample: Callable[[], Awaitable[JsonifyValue]]) -> None:
        self.sample = sample
        self.queues: set[asyncio.Queue[str]] = set()
        self.task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        while True:
            try:
                obj = await self.sample()
                frame = "data: " + json.dumps({"success": True, "msg": None, "obj": obj}) + "\n\n"
            except Exception:
                # Keep connections alive and allow the next sample to recover.
                frame = 'data: {"success": false, "msg": "sampling failed", "obj": null}\n\n'
            for queue in self.queues:
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(frame)
            await asyncio.sleep(1)

    async def stream(self) -> AsyncGenerator[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        self.queues.add(queue)
        if self.task is None:
            self.task = asyncio.create_task(self._run(), name="live-state")
        try:
            yield "retry: 2000\n\n"
            while True:
                try:
                    async with asyncio.timeout(15):
                        frame = await queue.get()
                except TimeoutError:
                    frame = ": heartbeat\n\n"
                yield frame
        finally:
            self.queues.discard(queue)
            if not self.queues:
                await self.close()

    async def close(self) -> None:
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
