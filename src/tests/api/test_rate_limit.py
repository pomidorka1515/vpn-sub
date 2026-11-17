from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from app_runtime import rate_limit
from app_runtime.rate_limit import AsyncRateLimiter
from core import Subscription

if TYPE_CHECKING:
    from quart import Response

    from app_runtime.rate_limit import RateLimitScript


class RedisStore:
    def __init__(self) -> None:
        self.events: dict[str, int] = {}
        self.closed = False
        self.failed = False
        self.calls = 0

    def register_script(self, script: str) -> RateLimitScript:
        if "INCRBY" in script:

            async def run(
                *, keys: tuple[str, ...], args: tuple[float, float, str, int, int]
            ) -> int:
                if self.failed:
                    raise OSError("Redis down")
                self.calls += 1
                increment, _, _, limit, _ = args
                count = self.events.get(keys[0], 0) + int(increment)
                self.events[keys[0]] = count
                return -60 if count > limit else count

            return run

        async def admin_run(
            *, keys: tuple[str, ...], args: tuple[float, float, str, int, int]
        ) -> int:
            return 0 if args[2] == "valid" else -1

        return admin_run

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [0.0]
    monkeypatch.setattr(rate_limit, "_monotonic", lambda: now[0])
    return now


@pytest.mark.asyncio
async def test_local_fixed_window_avoids_redis_on_hot_path(clock: list[float]) -> None:
    store = RedisStore()
    limiter = AsyncRateLimiter("redis://test", client=store)
    await limiter.start()
    assert await limiter.allow("route", "ip", 2)
    assert await limiter.allow("route", "ip", 2)
    assert not await limiter.allow("route", "ip", 2)
    assert store.calls == 0
    clock[0] = 60
    assert await limiter.allow("route", "ip", 2)
    await limiter.close()


@pytest.mark.asyncio
async def test_redis_reconciliation_is_batched_and_fail_open(clock: list[float]) -> None:
    store = RedisStore()
    limiter = AsyncRateLimiter(
        "redis://test",
        client=store,
        redis_check_interval=1,
    )
    await limiter.start()
    assert await limiter.allow("route", "ip", 3)
    clock[0] = 1.1
    assert await limiter.allow("route", "ip", 3)
    assert store.calls == 1
    assert list(store.events.values()) == [2]
    store.failed = True
    clock[0] = 2.2
    assert await limiter.allow("route", "ip", 3)
    assert not await limiter.allow("route", "ip", 3)
    await limiter.close()


@pytest.mark.asyncio
async def test_remote_denial_is_cached(clock: list[float]) -> None:
    store = RedisStore()
    limiter = AsyncRateLimiter("redis://test", client=store, redis_check_interval=1)
    await limiter.start()
    store.events[limiter._redis_key("route", "ip", 0)] = 2
    assert await limiter.allow("route", "ip", 3)
    clock[0] = 1.1
    assert not await limiter.allow("route", "ip", 3)
    assert store.calls == 1
    assert not await limiter.allow("route", "ip", 3)
    assert store.calls == 1
    await limiter.close()


@pytest.mark.asyncio
async def test_failure_is_fail_open() -> None:
    from api import AsyncBaseApi, rate_limit
    from api.common import Route

    class Handler(AsyncBaseApi):
        ROUTES = (Route("GET", "/test", "endpoint"),)

        @rate_limit(1)
        async def endpoint(self) -> tuple[Response, int]:
            from util import ok

            return ok()

    store = RedisStore()
    store.failed = True
    limiter = AsyncRateLimiter("redis://test", client=store)
    await limiter.start()
    app = Quart(__name__)
    handler = object.__new__(Handler)
    handler.limiter = limiter
    async with app.test_request_context("/test"):  # pyright: ignore[reportUnknownMemberType]  # Upstream auth parameter is untyped.
        assert (await handler.endpoint())[1] == 200
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
        assert len(limiter._local) == 2
    finally:
        await limiter.close()


@pytest.mark.asyncio
async def test_subscription_limits_token_and_ip(
    monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    from api.subscription import register_subscription_route
    from util import ok

    sub = MagicMock()
    sub.res.cfg.view.return_value = {"uri": "/sub/"}

    async def build(*args: object, **kwargs: object) -> tuple[Response, int]:
        return ok()

    builder = AsyncMock(side_effect=build)
    monkeypatch.setattr("api.subscription.get_subscription", builder)
    store = RedisStore()
    limiter = AsyncRateLimiter("redis://test", client=store)
    await limiter.start()
    app = Quart(__name__)
    register_subscription_route(app, cast(Subscription, sub), limiter)
    client = app.test_client()
    try:
        for _ in range(60):
            assert (await client.get("/sub?token=shared")).status_code == 200
        response = await client.get("/sub?token=shared")
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"
        assert (await client.get("/sub?token=other")).status_code == 200
        assert builder.await_count == 61
        for index in range(61, 599):
            assert (await client.get(f"/sub?token=aggregate-{index}")).status_code == 200
        response = await client.get("/sub?token=aggregate-overflow")
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"
        clock[0] = 60
        assert (await client.get("/sub?token=shared")).status_code == 200
    finally:
        await limiter.close()
