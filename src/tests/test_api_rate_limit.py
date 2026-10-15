from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from app_runtime.rate_limit import AsyncRateLimiter

if TYPE_CHECKING:
    from quart import Response

    from app_runtime.rate_limit import RateLimitScript
    from core import Subscription


class RedisStore:
    def __init__(self) -> None:
        self.events: dict[str, list[float]] = {}
        self.closed = False
        self.failed = False

    def register_script(self, script: str) -> RateLimitScript:
        assert "ZREMRANGEBYSCORE" in script
        assert "EXPIRE" in script

        async def run(*, keys: tuple[str, ...], args: tuple[float, float, str, int, int]) -> int:
            if len(keys) == 2:
                return 0 if args[2] == "valid" else -1
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
    monkeypatch.setattr(time, "time", lambda: 159.999)
    assert not await second.allow("route", "ip", 1)
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


@pytest.mark.parametrize("limit", [0, -1])
def test_rejects_non_positive_limit(limit: int) -> None:
    from api import rate_limit

    with pytest.raises(ValueError, match="max_requests must be positive"):
        rate_limit(limit)


@pytest.mark.asyncio
async def test_reused_decorator_and_unknown_address() -> None:
    from api import AsyncBaseApi, rate_limit

    decorator = rate_limit(1)

    class Handler(AsyncBaseApi):
        ROUTES = ()

        @decorator
        async def first(self) -> str:
            return "first"

        @decorator
        async def second(self) -> str:
            return "second"

    store = RedisStore()
    limiter = AsyncRateLimiter("redis://test", client=store)
    await limiter.start()
    handler = object.__new__(Handler)
    handler.limiter = limiter
    app = Quart(__name__)
    try:
        async with app.test_request_context("/test", scope_base={"client": None}):  # pyright: ignore[reportUnknownMemberType]  # Upstream auth parameter is untyped.
            assert await handler.first() == "first"
            assert await handler.second() == "second"
            denied = await handler.first()
            assert isinstance(denied, tuple)
            assert denied[1] == 429
        assert len(store.events) == 2
        assert all(key.endswith(":<unknown>") for key in store.events)
    finally:
        await limiter.close()


@pytest.mark.asyncio
async def test_subscription_limit_per_ip_and_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.subscription import register_subscription_route
    from util import ok

    sub = MagicMock()
    sub.res.cfg.view.return_value = {"uri": "/sub/"}

    def build(*args: object, **kwargs: object) -> tuple[Response, int]:
        return ok()

    builder = AsyncMock(side_effect=build)
    monkeypatch.setattr("api.subscription.get_subscription", builder)
    monkeypatch.setattr(time, "time", lambda: 100)
    store = RedisStore()
    limiter = AsyncRateLimiter("redis://test", client=store)
    await limiter.start()
    app = Quart(__name__)
    register_subscription_route(app, cast("Subscription", sub), limiter)
    client = app.test_client()
    try:
        for index in range(60):
            assert (await client.get(f"/sub?token={index}")).status_code == 200
        assert (await client.get("/sub/")).status_code == 429
        assert builder.await_count == 60
        assert (await client.get("/sub", scope_base={"client": ("other-ip", 1234)})).status_code == 200
        monkeypatch.setattr(time, "time", lambda: 160)
        assert (await client.get("/sub")).status_code == 200
        store.failed = True
        assert (await client.get("/sub")).status_code == 429
        assert builder.await_count == 62
    finally:
        await limiter.close()
