from __future__ import annotations

from collections.abc import Iterator
from flask.ctx import RequestContext
from unittest import mock

import pytest
from flask import Flask

from api import BaseApi, rate_limit
from api.decorators.rate_limit import (  # pyright: ignore[reportPrivateUsage]
    _RateLimitScript,
    _replace_client,
    close_rate_limit,
)


@pytest.fixture
def flask_app() -> Flask:
    return Flask(__name__)


def app_context(app: Flask, remote_addr: str | None) -> RequestContext:
    return app.test_request_context(
        "/",
        environ_base={"REMOTE_ADDR": remote_addr} if remote_addr is not None else {},
    )


class _SharedRedis:
    """Enough of a Redis sorted set to run the rate-limit script in-process."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sets: dict[str, dict[str, float]] = {}
        self.closed = False

    def register_script(self, script: str) -> _RateLimitScript:
        assert "ZADD" in script

        def run(keys: tuple[str, ...], args: tuple[float, float, str, int, int]) -> int:
            if self.fail:
                raise ConnectionError("redis down")
            key = keys[0]
            now, window, member, limit, _ttl = args
            bucket = self.sets.get(key, {})
            cutoff = now - window
            bucket = {
                item: score for item, score in bucket.items() if score > cutoff
            }
            if len(bucket) < limit:
                bucket[member] = now
                self.sets[key] = bucket
                return 1
            if bucket:
                self.sets[key] = bucket
            else:
                self.sets.pop(key, None)
            return 0

        return run

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def configured_rate_limit() -> Iterator[_SharedRedis]:
    client = _SharedRedis()
    _replace_client(client)
    yield client
    close_rate_limit()


def test_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError):
        rate_limit(0)


def test_limits_each_ip_independently(
    flask_app: Flask,
    configured_rate_limit: _SharedRedis,
) -> None:
    class Handler(BaseApi):
        ROUTES = ()

        @rate_limit(2)
        def endpoint(self: BaseApi) -> tuple[str, int]:
            return "ok", 200

    handler = object.__new__(Handler)
    with app_context(flask_app, "203.0.113.1"):
        with mock.patch("api.decorators.rate_limit.time.time", side_effect=[0.0, 1.0, 2.0]):
            assert handler.endpoint() == ("ok", 200)
            assert handler.endpoint() == ("ok", 200)
            _, status = handler.endpoint()
    assert status == 429

    with app_context(flask_app, "203.0.113.2"):
        with mock.patch("api.decorators.rate_limit.time.time", return_value=2.0):
            assert handler.endpoint() == ("ok", 200)
    assert len(configured_rate_limit.sets) == 2


def test_reused_decorator_gives_each_endpoint_its_own_window(
    flask_app: Flask,
    configured_rate_limit: _SharedRedis,
) -> None:
    limiter_decorator = rate_limit(1)

    class Handler(BaseApi):
        ROUTES = ()

        @limiter_decorator
        def first(self: BaseApi) -> tuple[str, int]:
            return "first", 200

        @limiter_decorator
        def second(self: BaseApi) -> tuple[str, int]:
            return "second", 200

    handler = object.__new__(Handler)
    with app_context(flask_app, "203.0.113.4"):
        with mock.patch("api.decorators.rate_limit.time.time", return_value=0.0):
            assert handler.first() == ("first", 200)
            assert handler.second() == ("second", 200)
    assert len(configured_rate_limit.sets) == 2


def test_missing_remote_address_uses_shared_unknown_bucket(
    flask_app: Flask,
    configured_rate_limit: _SharedRedis,
) -> None:
    class Handler(BaseApi):
        ROUTES = ()

        @rate_limit(1)
        def endpoint(self: BaseApi) -> tuple[str, int]:
            return "ok", 200

    handler = object.__new__(Handler)
    with app_context(flask_app, None):
        with mock.patch("api.decorators.rate_limit.time.time", side_effect=[0.0, 1.0]):
            assert handler.endpoint() == ("ok", 200)
            assert handler.endpoint()[1] == 429
    assert any(key.endswith(":<unknown>") for key in configured_rate_limit.sets)


def test_redis_window_is_shared_across_limiter_instances(
    flask_app: Flask,
    configured_rate_limit: _SharedRedis,
) -> None:
    class Handler(BaseApi):
        ROUTES = ()

        @rate_limit(1)
        def endpoint(self: BaseApi) -> tuple[str, int]:
            return "ok", 200

    first = object.__new__(Handler)
    second = object.__new__(Handler)
    with app_context(flask_app, "203.0.113.9"):
        with mock.patch("api.decorators.rate_limit.time.time", return_value=10.0):
            assert first.endpoint() == ("ok", 200)
            assert second.endpoint()[1] == 429
    assert len(configured_rate_limit.sets) == 1


def test_redis_window_expires_at_sixty_seconds(
    flask_app: Flask,
    configured_rate_limit: _SharedRedis,
) -> None:
    class Handler(BaseApi):
        ROUTES = ()

        @rate_limit(1)
        def endpoint(self: BaseApi) -> tuple[str, int]:
            return "ok", 200

    handler = object.__new__(Handler)
    with app_context(flask_app, "203.0.113.10"):
        with mock.patch("api.decorators.rate_limit.time.time", side_effect=[0.0, 59.999, 60.0]):
            assert handler.endpoint() == ("ok", 200)
            assert handler.endpoint()[1] == 429
            assert handler.endpoint() == ("ok", 200)
    assert configured_rate_limit.sets


def test_redis_failure_fails_closed(flask_app: Flask) -> None:
    client = _SharedRedis(fail=True)
    _replace_client(client)
    try:
        class Handler(BaseApi):
            ROUTES = ()

            @rate_limit(5)
            def endpoint(self: BaseApi) -> tuple[str, int]:
                return "ok", 200

        handler = object.__new__(Handler)
        with app_context(flask_app, "203.0.113.11"):
            assert handler.endpoint()[1] == 429
    finally:
        close_rate_limit()
    assert client.closed
