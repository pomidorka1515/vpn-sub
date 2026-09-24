from __future__ import annotations

from typing import Any, cast
from unittest import mock

import pytest
import uuid
from argon2 import PasswordHasher
from flask import Flask

from api import Api, BaseApi, WebApi, _RateLimiter, rate_limit  # pyright: ignore[reportPrivateUsage]
from config import ConfigLike, LinesConfigLike
from db import Database
from errors import AppError
from helpers import make_subscription, make_watch, subscription_config


@pytest.fixture
def flask_app() -> Flask:
    return Flask(__name__)


@pytest.fixture
def web_api(database: Database, flask_app: Flask) -> tuple[Flask, Database]:
    password_hash = PasswordHasher().hash("secret")
    subscription = make_subscription(database, app=flask_app)
    database.create_user(
        username="alice", uuid=str(uuid.uuid4()), token="a" * 40,
        fingerprint="chrome", displayname="Alice",
        ext_username="alice-login", ext_password_hash=password_hash,
    )
    subscription.password_svc.hash = lambda value: password_hash  # type: ignore[assignment]
    watcher = make_watch(database, subscription)
    WebApi(
        app=flask_app, cfg=cast(ConfigLike, subscription_config()),
        sub=subscription, bw=watcher,
    )
    return flask_app, database


class _Audit:
    path = "audit.jsonl"
    size = 0

    def append(self, record: object) -> None:
        del record

    def append_many(self, records: object) -> None:
        del records

    def __iter__(self) -> object:
        return iter(())

    def tail(self, n: int = 100) -> object:
        del n
        return iter(())

    def read_all(self) -> list[object]:
        return []

    def first(self, n: int = 1) -> list[object]:
        del n
        return []

    def count(self) -> int:
        return 0


def test_user_refresh_aborts_on_non_panel_error(database: Database, flask_app: Flask) -> None:
    subscription = make_subscription(database, app=flask_app)
    database.create_user(
        username="alice", uuid="01234567-89ab-cdef-0123-456789abcdef", token="a" * 40,
        fingerprint="chrome", displayname="Alice",
    )
    database.create_user(
        username="bob", uuid="11234567-89ab-cdef-0123-456789abcdef", token="b" * 40,
        fingerprint="chrome", displayname="Bob",
    )

    def add_users(username: str, _called_internally: bool = False) -> None:
        del _called_internally
        if username == "bob":
            raise AppError("panel rejected")

    subscription.business_svc.add_users = add_users  # type: ignore[method-assign]
    Api(
        app=flask_app,
        cfg=cast(ConfigLike, subscription_config(api_uri="api", api_token="secret")),
        audit_cfg=cast(LinesConfigLike, _Audit()),
        sub=subscription,
        bw=make_watch(database, subscription),
    )
    response = flask_app.test_client().get(
        "/sub/api/api/user/refresh",
        headers={"Authorization": "secret"},
    )
    assert response.status_code == 500
    payload = response.get_json()
    assert payload["msg"] == "Refresh aborted"
    assert payload["obj"]["aborted"] == "bob"
    assert payload["obj"]["failed"] == []
    assert payload["obj"]["succeeded"] == 1
    assert payload["obj"]["total"] == 2


def test_login_uses_isolated_auth_token(web_api: tuple[Flask, Database]) -> None:
    app, _database = web_api
    client = app.test_client()
    legacy_cookie_response = client.get(
        "/sub/webapi/stats", headers={"Cookie": "token=" + "a" * 40}
    )
    assert legacy_cookie_response.status_code == 401

    login_response = client.post(
        "/sub/webapi/login",
        json={"username": "alice-login", "password": "secret"},
    )
    assert login_response.status_code == 200
    cookies = login_response.headers.getlist("Set-Cookie")
    assert any(cookie.startswith("auth_token=") for cookie in cookies)
    assert any(cookie.startswith("token=;") for cookie in cookies)
    auth_cookie = next(cookie for cookie in cookies if cookie.startswith("auth_token="))
    assert "Max-Age=2592000" in auth_cookie
    assert "HttpOnly" in auth_cookie
    assert "Secure" in auth_cookie
    assert "SameSite=Lax" in auth_cookie
    auth_token = auth_cookie.split("=", 1)[1].split(";", 1)[0]
    assert len(auth_token) == 100
    assert auth_token != "a" * 40
    assert "auth_token" not in login_response.get_json()["obj"]

    stats_response = client.get("/sub/webapi/stats")
    assert stats_response.status_code == 200
    assert stats_response.get_json()["obj"]["token"] == "a" * 40


def test_logout_invalidates_auth_token(web_api: tuple[Flask, Database]) -> None:
    app, database = web_api
    database.set_auth_token("alice", "a" * 100)
    client = app.test_client()
    client.set_cookie("auth_token", "a" * 100)
    response = client.post("/sub/webapi/logout")
    assert response.status_code == 200
    assert response.headers["Set-Cookie"].count("auth_token=") == 1
    record = database.get_user("alice")
    assert record is not None
    assert record["auth_token"] is None
    assert client.get("/sub/webapi/stats").status_code == 401


def app_context(app: Flask, remote_addr: str | None) -> Any:
    return app.test_request_context(
        "/",
        environ_base={"REMOTE_ADDR": remote_addr} if remote_addr is not None else {},
    )


def test_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError):
        rate_limit(0)
    with pytest.raises(ValueError):
        rate_limit(1, max_buckets=0)


def test_limits_each_ip_independently_without_instance_state(flask_app: Flask) -> None:
    class Handler(BaseApi):
        ROUTES = []

        @rate_limit(2)
        def endpoint(self: BaseApi) -> tuple[str, int]:
            return "ok", 200

    handler = object.__new__(Handler)
    with app_context(flask_app, "203.0.113.1"):
        with mock.patch("api.decorators.rate_limit.time.monotonic", side_effect=[0.0, 1.0, 2.0]):
            assert handler.endpoint() == ("ok", 200)
            assert handler.endpoint() == ("ok", 200)
            _, status = handler.endpoint()
    assert status == 429

    with app_context(flask_app, "203.0.113.2"):
        with mock.patch("api.decorators.rate_limit.time.monotonic", return_value=2.0):
            assert handler.endpoint() == ("ok", 200)


def test_window_expires_at_sixty_seconds(flask_app: Flask) -> None:
    class Handler(BaseApi):
        ROUTES = []

        @rate_limit(1)
        def endpoint(self: BaseApi) -> tuple[str, int]:
            return "ok", 200

    handler = object.__new__(Handler)
    with app_context(flask_app, "203.0.113.3"):
        with mock.patch("api.decorators.rate_limit.time.monotonic", side_effect=[0.0, 59.999, 60.0]):
            assert handler.endpoint() == ("ok", 200)
            assert handler.endpoint()[1] == 429
            assert handler.endpoint() == ("ok", 200)


def test_inactive_buckets_are_evicted_without_global_scan() -> None:
    limiter = _RateLimiter(1)
    assert limiter.allow("first", 0.0)
    assert limiter.allow("second", 1.0)
    assert limiter.bucket_count == 2
    assert limiter.allow("third", 62.0)
    assert limiter.bucket_keys() == ("third",)


def test_rejected_request_does_not_break_eviction_order() -> None:
    limiter = _RateLimiter(1)
    assert limiter.allow("first", 0.0)
    assert limiter.allow("second", 1.0)
    assert not limiter.allow("first", 59.0)
    assert limiter.allow("third", 60.5)
    assert limiter.bucket_keys() == ("second", "third")


def test_reused_decorator_gives_each_endpoint_its_own_limiter(flask_app: Flask) -> None:
    limiter_decorator = rate_limit(1)

    class Handler(BaseApi):
        ROUTES = []

        @limiter_decorator
        def first(self: BaseApi) -> tuple[str, int]:
            return "first", 200

        @limiter_decorator
        def second(self: BaseApi) -> tuple[str, int]:
            return "second", 200

    handler = object.__new__(Handler)
    with app_context(flask_app, "203.0.113.4"):
        with mock.patch("api.decorators.rate_limit.time.monotonic", return_value=0.0):
            assert handler.first() == ("first", 200)
            assert handler.second() == ("second", 200)


def test_bucket_count_is_capped() -> None:
    limiter = _RateLimiter(1, max_buckets=2)
    assert limiter.allow("first", 0.0)
    assert limiter.allow("second", 1.0)
    assert limiter.allow("third", 2.0)
    assert limiter.bucket_count == 2
    assert limiter.bucket_keys() == ("second", "third")


def test_missing_remote_address_uses_shared_unknown_bucket(flask_app: Flask) -> None:
    class Handler(BaseApi):
        ROUTES = []

        @rate_limit(1)
        def endpoint(self: BaseApi) -> tuple[str, int]:
            return "ok", 200

    handler = object.__new__(Handler)
    with app_context(flask_app, None):
        with mock.patch("api.decorators.rate_limit.time.monotonic", side_effect=[0.0, 1.0]):
            assert handler.endpoint() == ("ok", 200)
            assert handler.endpoint()[1] == 429
