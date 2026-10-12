from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
from quart import Quart

from api.decorators.rate_limit import AsyncRateLimiter

if TYPE_CHECKING:
    from quart import Response

    from api.decorators.rate_limit import RateLimitScript


class RedisStore:
    def __init__(self) -> None:
        self.events: dict[str, list[float]] = {}
        self.closed = False
        self.failed = False

    def register_script(self, script: str) -> RateLimitScript:
        assert "ZREMRANGEBYSCORE" in script
        assert "EXPIRE" in script

        async def run(*, keys: tuple[str, ...], args: tuple[float, float, str, int, int]) -> int:
            if self.failed:
                raise OSError("Redis down")
            now, window, _, limit, ttl = args
            assert ttl == 60
            events = self.events.setdefault(keys[0], [])
            events[:] = [stamp for stamp in events if stamp > now - window]
            if len(events) >= limit:
                return 0
            events.append(now)
            return 1

        return run

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_shared_windows_scope_ip_and_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    store = RedisStore()
    first = AsyncRateLimiter("redis://test", client=store)
    second = AsyncRateLimiter("redis://test", client=store)
    await first.start()
    await second.start()
    monkeypatch.setattr(time, "time", lambda: 100)
    assert await first.allow("route", "ip", 1)
    assert not await second.allow("route", "ip", 1)
    assert await second.allow("other", "ip", 1)
    assert await second.allow("route", "other-ip", 1)
    monkeypatch.setattr(time, "time", lambda: 160)
    assert await second.allow("route", "ip", 1)
    await first.close()
    await second.close()
    assert not store.closed


@pytest.mark.asyncio
async def test_failure_is_fail_closed() -> None:

    from api import AsyncBaseApi, rate_limit
    from api.common import Route

    class Handler(AsyncBaseApi):
        ROUTES = (Route("GET", "/test", "endpoint"),)

        @rate_limit(1)
        async def endpoint(self) -> tuple[Response, int]:
            from util import ok

            return ok()

    store = RedisStore()
    limiter = AsyncRateLimiter("redis://test", client=store)
    await limiter.start()
    app = Quart(__name__)
    handler = object.__new__(Handler)
    handler.limiter = limiter
    async with app.test_request_context("/test"):  # pyright: ignore[reportUnknownMemberType]  # Upstream auth parameter is untyped.
        assert (await handler.endpoint())[1] == 200
        assert (await handler.endpoint())[1] == 429
    store.failed = True
    async with app.test_request_context("/test"):  # pyright: ignore[reportUnknownMemberType]  # Upstream auth parameter is untyped.
        assert (await handler.endpoint())[1] == 429
    await limiter.close()
