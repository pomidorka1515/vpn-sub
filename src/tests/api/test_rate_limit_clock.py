from __future__ import annotations

import asyncio
import shutil
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
                raise RuntimeError("test Redis failed to start") from None
            await asyncio.sleep(0.01)
        await limiter.start()
        yield limiter, client
    finally:
        await limiter.close()
        await client.aclose()
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=5)


@pytest.mark.asyncio
async def test_fixed_window_script_uses_increment_and_expiry(
    redis_store: tuple[AsyncRateLimiter, Redis],
) -> None:
    _, client = redis_store
    script = client.register_script(_RATE_LIMIT_SCRIPT)
    key = "ratelimit:test:fixed"

    async def check(increment: int, limit: int = 2) -> int:
        return int(await script(keys=[key], args=[increment, 60, "", limit, 60]))

    assert await check(1) == 1
    assert await check(1) == 2
    assert await check(1) < 0
    assert await client.get(key) == "3"
    assert 0 < await client.ttl(key) <= 60


@pytest.mark.asyncio
async def test_simultaneous_fixed_window_increments_are_atomic(
    redis_store: tuple[AsyncRateLimiter, Redis],
) -> None:
    _, client = redis_store
    script = client.register_script(_RATE_LIMIT_SCRIPT)
    key = "ratelimit:test:concurrent"

    async def check() -> int:
        return int(await script(keys=[key], args=[1, 60, "", 10, 60]))

    results = await asyncio.gather(*(check() for _ in range(30)))
    assert sum(result > 0 for result in results) == 10
    assert await client.get(key) == "30"


@pytest.mark.asyncio
async def test_local_requests_do_not_touch_redis_before_reconciliation(
    redis_store: tuple[AsyncRateLimiter, Redis],
) -> None:
    limiter, client = redis_store
    limiter.redis_check_interval = 60
    for _ in range(3):
        assert await limiter.allow("route", "local", 10)
    assert await client.keys("ratelimit:*") == []  # pyright: ignore[reportUnknownMemberType]  # Upstream Redis typing is partial.
