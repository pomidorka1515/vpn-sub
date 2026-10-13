from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from api.admin.live import LiveState

if TYPE_CHECKING:
    from custom_types import JsonifyValue

pytestmark = pytest.mark.asyncio


async def test_live_sampler_shared_and_stops_with_last_viewer() -> None:
    calls = 0

    async def sample() -> JsonifyValue:
        nonlocal calls
        calls += 1
        return {"sample": calls}

    live = LiveState(sample)
    first, second = live.stream(), live.stream()
    try:
        assert await anext(first) == "retry: 2000\n\n"
        task = live.task
        assert await anext(second) == "retry: 2000\n\n"
        assert live.task is task
        async with asyncio.timeout(5):
            frames = await asyncio.gather(anext(first), anext(second))
        assert frames[0] == frames[1]
        assert calls == 1
        await first.aclose()
        assert live.task is task
        await second.aclose()
        assert live.task is None
        assert task is not None
        assert task.cancelled()
        assert not live.queues
    finally:
        await first.aclose()
        await second.aclose()
        await live.close()


async def test_live_sampler_recovers_and_bounds_slow_viewers() -> None:
    calls = 0
    sampled = asyncio.Event()

    async def sample() -> JsonifyValue:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("secret")
        if calls >= 4:
            sampled.set()
        return {"sample": calls}

    live = LiveState(sample)
    stream = live.stream()
    try:
        await anext(stream)
        async with asyncio.timeout(5):
            failed = await anext(stream)
            assert "secret" not in failed
            assert json.loads(failed.removeprefix("data: "))["success"] is False
            recovered = await anext(stream)
            assert json.loads(recovered.removeprefix("data: "))["success"] is True
            # A paused viewer retains only the freshest sample.
            await sampled.wait()
        assert all(queue.qsize() == 1 for queue in live.queues)
        frame = await anext(stream)
        assert json.loads(frame.removeprefix("data: "))["obj"]["sample"] == calls
    finally:
        await stream.aclose()
        await live.close()
