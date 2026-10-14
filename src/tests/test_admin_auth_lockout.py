from __future__ import annotations

import asyncio
import shutil
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from quart import Quart, Response
from redis.asyncio import Redis

from api.admin.routes import AsyncApi
from api.decorators.auth import requires_admin_auth
from app_runtime.rate_limit import AsyncRateLimiter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from api.common import ResponseType


@pytest_asyncio.fixture
async def auth_store(tmp_path: Path) -> AsyncIterator[tuple[AsyncRateLimiter, Redis]]:
    socket = tmp_path / "redis.sock"
    executable = shutil.which("redis-server")
    if executable is None:
        pytest.skip("redis-server is required for Redis script integration tests")
    process = await asyncio.create_subprocess_exec(
        executable, "--port", "0", "--unixsocket", str(socket), "--save", "", "--appendonly", "no",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    client = Redis(unix_socket_path=str(socket), decode_responses=True)
    limiter = AsyncRateLimiter(f"unix://{socket}")
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
async def test_atomic_lockout_reset_expiry_and_ip_isolation(
    auth_store: tuple[AsyncRateLimiter, Redis],
) -> None:
    limiter, client = auth_store
    for _ in range(9):
        assert await limiter.admin_auth("ip", valid=False) == -1
    assert await limiter.admin_auth("ip", valid=True) == 0
    assert await limiter.admin_auth("ip", valid=False) == -1
    # Failures older than the rolling window are removed by Redis's clock.
    await client.zadd("admin-auth:{ip}:failures", {"old": 0})
    results = await asyncio.gather(*(limiter.admin_auth("ip", valid=False) for _ in range(9)))
    assert results.count(-1) == 8
    assert results.count(900) == 1
    assert await limiter.admin_auth("other-ip", valid=True) == 0
    await client.expire("admin-auth:{ip}:lockout", 30)
    assert 0 < await limiter.admin_auth("ip", valid=True) <= 30
    assert 0 < await limiter.admin_auth("ip", valid=False) <= 30
    assert await client.ttl("admin-auth:{ip}:lockout") <= 30
    await client.pexpire("admin-auth:{ip}:lockout", 1)
    await asyncio.sleep(0.01)
    assert await limiter.admin_auth("ip", valid=True) == 0
    assert await client.exists("admin-auth:{ip}:failures") == 0


@pytest.mark.asyncio
async def test_routes_share_lockout_and_fail_closed(
    auth_store: tuple[AsyncRateLimiter, Redis],
) -> None:
    limiter, _ = auth_store

    class Handler(AsyncApi):
        @requires_admin_auth
        async def first(self) -> ResponseType:
            return Response("first")

        @requires_admin_auth
        async def second(self) -> ResponseType:
            return Response("second")

    handler = object.__new__(Handler)
    handler.limiter = limiter
    handler.token = "test-admin-token-at-least-20"
    app = Quart(__name__)
    async with app.test_request_context(  # pyright: ignore[reportUnknownMemberType]
        "/first", headers={"Authorization": handler.token},
    ):
        response = await handler.first()
        assert isinstance(response, Response)
        assert response.status_code == 200
    async with app.test_request_context("/first"):  # pyright: ignore[reportUnknownMemberType]
        for _ in range(9):
            response = await handler.first()
            assert isinstance(response, tuple)
            assert response[1] == 401
        response = await handler.second()
        assert isinstance(response, tuple)
        assert response[1] == 429
        assert response[0].headers["Retry-After"] == "900"
    async with app.test_request_context(  # pyright: ignore[reportUnknownMemberType]
        "/second", headers={"Authorization": handler.token},
    ):
        response = await handler.second()
        assert isinstance(response, tuple)
        assert response[1] == 429
        await limiter.close()
        response = await handler.second()
        assert isinstance(response, tuple)
        assert response[1] == 503
