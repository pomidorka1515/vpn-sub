from __future__ import annotations

import asyncio
import shutil
import time
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from app_runtime.rate_limit import _RATE_LIMIT_SCRIPT, AsyncRateLimiter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

pytestmark = [pytest.mark.integration, pytest.mark.redis]


@pytest_asyncio.fixture
async def redis_store(tmp_path: Path) -> AsyncIterator[tuple[AsyncRateLimiter, Redis]]:
    socket = tmp_path / "redis.sock"
    executable = shutil.which("redis-server")
    if executable is None:
        pytest.skip("redis-server is required for Redis script integration tests")
    process = await asyncio.create_subprocess_exec(
        executable,
        "--port",
        "0",
        "--unixsocket",
        str(socket),
        "--save",
        "",
        "--appendonly",
        "no",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    client = Redis(unix_socket_path=str(socket), decode_responses=True)
    limiter = AsyncRateLimiter(f"unix://{socket}", socket_timeout=5)
    try:
        for _ in range(100):
            if socket.exists():
                break
            if process.returncode is not None:
                raise RuntimeError("test Redis failed to start")
            await asyncio.sleep(0.01)
        await limiter.start()
        yield limiter, client
    finally:
        await limiter.close()
        await client.aclose()
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=5)


@pytest.mark.asyncio
async def test_app_clock_jumps_do_not_affect_real_redis_windows(
    redis_store: tuple[AsyncRateLimiter, Redis],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limiter, client = redis_store
    monkeypatch.setattr(time, "time", lambda: 100.0)
    assert await limiter.allow("route", "ip", 1)
    monkeypatch.setattr(time, "time", lambda: 10_000_000_000.0)
    assert not await limiter.allow("route", "ip", 1)
    monkeypatch.setattr(time, "time", lambda: 1.0)
    assert not await limiter.allow("route", "ip", 1)
    key = "ratelimit:route:ip"
    await client.expire(key, 5)
    assert not await limiter.allow("route", "ip", 1)
    assert 0 < await client.ttl(key) <= 5
    assert await limiter.allow("route", "other", 1)


@pytest.mark.asyncio
async def test_simultaneous_requests_are_counted_once_each(
    redis_store: tuple[AsyncRateLimiter, Redis],
) -> None:
    limiter, client = redis_store
    results = await asyncio.gather(*(limiter.allow("route", "concurrent", 10) for _ in range(30)))
    assert results.count(True) == 10
    assert await client.zcard("ratelimit:route:concurrent") == 10


@pytest.mark.asyncio
async def test_redis_clock_discontinuities_rebase_and_expire_atomically(
    redis_store: tuple[AsyncRateLimiter, Redis],
) -> None:
    _, client = redis_store
    # Run the production Lua unchanged except for injecting TIME's two values.
    clock_call = "local clock = redis.call('TIME')"
    assert _RATE_LIMIT_SCRIPT.count(clock_call) == 1
    script = client.register_script(
        _RATE_LIMIT_SCRIPT.replace(clock_call, "local clock = {ARGV[6], ARGV[7]}")
    )
    key = "ratelimit:route:clock"

    async def allow(seconds: int, micros: int, member: str) -> int:
        return int(await script(keys=[key], args=[0, 60, member, 2, 60, seconds, micros]))

    assert await allow(1_000, 500_000, "a") == 1
    assert await allow(1_001, 0, "b") == 1
    await client.expire(key, 5)
    assert await allow(900, 250_000, "denied") == 0
    assert await client.zcard(key) == 2
    assert await client.zscore(key, "a") == 900.25
    assert await client.zscore(key, "b") == 900.25
    assert await client.ttl(key) > 5
    await client.expire(key, 5)
    assert await allow(960, 249_999, "still-denied") == 0
    assert 0 < await client.ttl(key) <= 5
    assert await allow(960, 250_000, "c") == 1
    assert await allow(10_000, 0, "d") == 1
    assert await client.zcard(key) == 1
    assert await client.zscore(key, "d") == 10_000.0
