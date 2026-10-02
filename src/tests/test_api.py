from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, cast
from unittest import mock

import pytest
import uuid
from argon2 import PasswordHasher
from flask import Flask
from pathlib import Path

from api import Api, BaseApi, WebApi, rate_limit
from api.decorators.rate_limit import (  # pyright: ignore[reportPrivateUsage]
    _replace_client,
    close_rate_limit,
)
from api.common import RES_DIR
from config import Config, ConfigLike, LinesConfigLike
from db import Database
from errors import AppError, DatabaseError
from helpers import make_subscription, make_watch, subscription_config
from jinja2 import FileSystemLoader

_LANG_PATH = Path(__file__).resolve().parents[2] / "lang.jsonc"


def _web_lang_cfg() -> ConfigLike:
    return cast(ConfigLike, Config(path=_LANG_PATH, read_only=True, read_only_jsonc=True))


@pytest.fixture
def flask_app() -> Flask:
    app = Flask(__name__)
    # jinja_loader is a cached_property; assigning replaces the template lookup.
    cast(Any, app).jinja_loader = FileSystemLoader(str(RES_DIR))
    return app


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


def _admin_api(
    database: Database,
    flask_app: Flask,
    *,
    api_uri: str = "api",
    api_admin_ui_auth: tuple[str, str] = ("admin", "panel-secret"),
) -> None:
    subscription = make_subscription(database, app=flask_app, lang_cfg=_web_lang_cfg())
    Api(
        app=flask_app,
        cfg=cast(ConfigLike, subscription_config(
            api_uri=api_uri,
            api_token="secret",
            api_admin_ui_auth=list(api_admin_ui_auth),
        )),
        audit_cfg=cast(LinesConfigLike, _Audit()),
        sub=subscription,
        bw=make_watch(database, subscription),
    )


def test_admin_ui_requires_session(database: Database, flask_app: Flask) -> None:
    _admin_api(database, flask_app)
    client = flask_app.test_client()
    denied = client.get("/sub/admin")
    assert denied.status_code == 302
    assert denied.headers["Location"].endswith("/sub/admin/login")

    login_page = client.get("/sub/admin/login")
    assert login_page.status_code == 200
    assert b'id="loginForm"' in login_page.data
    assert b'id="tabRegister"' not in login_page.data
    assert b'id="registerForm"' not in login_page.data
    assert b"const SESSION = PANEL + '/session'" in login_page.data
    assert b"PANEL + '/token'" in login_page.data
    assert b"const API" not in login_page.data
    assert b"window.location.href = PANEL;" in login_page.data
    assert b"PANEL + '/'" not in login_page.data

    bad = client.post("/sub/admin/session", json={"username": "admin", "password": "nope"})
    assert bad.status_code == 401
    short = client.post("/sub/admin/session", json={"username": "admin", "password": "x"})
    assert short.status_code == 401
    missing = client.post("/sub/admin/session", json={"username": "admin"})
    assert missing.status_code == 401
    empty = client.post("/sub/admin/session", json={"username": "", "password": ""})
    assert empty.status_code == 401
    not_json = client.post("/sub/admin/session", data="username=admin", content_type="text/plain")
    assert not_json.status_code == 400

    signed_in = client.post(
        "/sub/admin/session", json={"username": "admin", "password": "panel-secret"},
    )
    assert signed_in.status_code == 200
    assert "admin_ui=" in signed_in.headers["Set-Cookie"]
    assert "Path=/sub/admin" in signed_in.headers["Set-Cookie"]
    assert "HttpOnly" in signed_in.headers["Set-Cookie"]
    first = database.admin_ui_session()
    assert first is not None and len(first) == 100

    response = client.get("/sub/admin")
    assert response.status_code == 200
    assert client.get("/sub/admin/").status_code == 200
    assert b"<html" in response.data.lower()
    assert b'href="/sub/common.css?v=' in response.data
    assert b'href="/sub/admin.css?v=' in response.data
    assert b'type="module"' in response.data
    assert b'src="/sub/admin/main.js?v=' in response.data
    assert b'id="btnLogout"' in response.data

    again = client.post(
        "/sub/admin/session", json={"username": "admin", "password": "panel-secret"},
    )
    assert again.status_code == 200
    second = database.admin_ui_session()
    assert second is not None and second != first
    client.delete_cookie("admin_ui", path="/sub/admin")
    client.set_cookie("admin_ui", first, path="/sub/admin")
    assert client.get("/sub/admin").status_code == 302

    logged_out = client.post("/sub/admin/logout")
    assert logged_out.status_code == 200
    assert "Path=/sub/admin" in logged_out.headers["Set-Cookie"]
    assert "Max-Age=0" in logged_out.headers["Set-Cookie"]
    assert database.admin_ui_session() is None
    assert client.get("/sub/admin").status_code == 302
    assert client.get("/sub/admin/token").status_code == 401


def test_admin_logout_cookie_path_matches_login_when_uri_empty(
    database: Database, flask_app: Flask,
) -> None:
    subscription = make_subscription(database, app=flask_app, lang_cfg=_web_lang_cfg())
    Api(
        app=flask_app,
        cfg=cast(ConfigLike, subscription_config(
            uri="",
            api_uri="api",
            api_token="secret",
            api_admin_ui_auth=["admin", "panel-secret"],
        )),
        audit_cfg=cast(LinesConfigLike, _Audit()),
        sub=subscription,
        bw=make_watch(database, subscription),
    )
    client = flask_app.test_client()
    signed_in = client.post("/admin/session", json={"username": "admin", "password": "panel-secret"})
    assert signed_in.status_code == 200
    assert "Path=/admin" in signed_in.headers["Set-Cookie"]
    logged_out = client.post("/admin/logout")
    assert logged_out.status_code == 200
    assert "Path=/admin" in logged_out.headers["Set-Cookie"]
    assert "Max-Age=0" in logged_out.headers["Set-Cookie"]


def test_admin_token_returns_secret_and_api_root(database: Database, flask_app: Flask) -> None:
    _admin_api(database, flask_app)
    client = flask_app.test_client()
    denied = client.get("/sub/admin/token")
    assert denied.status_code == 401
    client.post("/sub/admin/session", json={"username": "admin", "password": "panel-secret"})
    response = client.get("/sub/admin/token")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["obj"]["token"] == "secret"
    assert payload["obj"]["api_root"] == "/sub/api"


def test_admin_token_api_root_omits_empty_api_uri(database: Database, flask_app: Flask) -> None:
    _admin_api(database, flask_app, api_uri="")
    client = flask_app.test_client()
    client.post("/sub/admin/session", json={"username": "admin", "password": "panel-secret"})
    response = client.get("/sub/admin/token")
    assert response.status_code == 200
    assert response.get_json()["obj"]["api_root"] == "/sub"


def test_polling_status_returns_limited_dynamic_state(
    database: Database, flask_app: Flask, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_types import (
        AppMemory, ConnCount, LoadAverage, NetTrafficStats,
        PollingSystemInfo, RamInfo, SwapInfo, SystemMemory,
    )
    from helpers import FakePanel

    host = PollingSystemInfo(
        cpu=1.5,
        process_count=4,
        uptime=1000.5,
        loadavg=LoadAverage(load_1m=0.1, load_5m=0.2, load_15m=0.3),
        network=NetTrafficStats(sent=1, recv=2),
        memory=SystemMemory(
            ram=RamInfo(total=3, available=2, used=1),
            swap=SwapInfo(total=0, free=0, used=0),
        ),
        connections=ConnCount(tcp=5, udp=6),
        app_memory=AppMemory(ram=7, swap=0),
        app_uptime=200.25,
        app_thread_amount=1,
        app_threads=(),
    )
    monkeypatch.setattr("api.admin.SysUtil.polling_info", lambda: host)

    panel = FakePanel(
        name="edge",
        status_payload={
            "success": True,
            "msg": "",
            "obj": {
                "cpu": 12.5,
                "cpuCores": 2,
                "logicalPro": 4,
                "cpuSpeedMhz": 2400,
                "mem": {"current": 10, "total": 20},
                "swap": {"current": 1, "total": 2},
                "disk": {"current": 3, "total": 4},
                "xray": {"state": "running", "errorMsg": "", "version": "1"},
                "uptime": 99,
                "loads": [0.1, 0.2, 0.3],
                "tcpCount": 7,
                "udpCount": 8,
                "netIO": {"up": 1, "down": 2},
                "netTraffic": {"sent": 9, "recv": 10},
                "publicIP": {"ipv4": "1.1.1.1", "ipv6": "::1"},
                "appStats": {"threads": 2, "mem": 3, "uptime": 4},
            },
        },
    )
    down = FakePanel(name="down", status_error=RuntimeError("down"))
    subscription = make_subscription(
        database, app=flask_app, lang_cfg=_web_lang_cfg(), panels=[panel, down],
    )
    Api(
        app=flask_app,
        cfg=cast(ConfigLike, subscription_config(api_uri="api", api_token="secret")),
        audit_cfg=cast(LinesConfigLike, _Audit()),
        sub=subscription,
        bw=make_watch(database, subscription),
    )
    client = flask_app.test_client()
    denied = client.get("/sub/api/api/state/polling")
    assert denied.status_code == 401

    response = client.get("/sub/api/api/state/polling", headers={"Authorization": "secret"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    obj = payload["obj"]
    assert set(obj) == {"host", "panels"}
    assert set(obj["host"]) == {
        "cpu", "connections", "network", "memory", "loadavg",
        "app_memory", "app_threads", "app_thread_amount", "process_count",
        "uptime", "app_uptime",
    }
    assert obj["host"]["cpu"] == 1.5
    assert obj["host"]["network"] == {"sent": 1, "recv": 2}
    assert obj["host"]["connections"] == {"tcp": 5, "udp": 6}
    assert obj["host"]["uptime"] == 1000.5
    assert obj["host"]["app_uptime"] == 200.25
    assert "cpu_info" not in obj["host"]
    assert "ip" not in obj["host"]
    assert set(obj["panels"]) == {"edge", "down"}
    assert obj["panels"]["down"] is None
    edge = obj["panels"]["edge"]
    assert set(edge) == {
        "app_stats", "cpu", "disk", "loads", "mem",
        "netIO", "netTraffic", "swap", "tcpCount", "udpCount", "uptime",
    }
    assert edge["cpu"] == 12.5
    assert edge["netIO"] == {"up": 1, "down": 2}
    assert edge["netTraffic"] == {"sent": 9, "recv": 10}
    assert edge["tcpCount"] == 7
    assert edge["udpCount"] == 8
    assert edge["uptime"] == 99
    assert edge["app_stats"] == {"threads": 2, "mem": 3, "uptime": 4}
    assert "xray" not in edge
    assert "publicIP" not in edge


def test_health_reports_db_and_process_status(database: Database, flask_app: Flask) -> None:
    _admin_api(database, flask_app)
    client = flask_app.test_client()
    denied = client.get("/sub/api/api/health")
    assert denied.status_code == 401
    response = client.get("/sub/api/api/health", headers={"Authorization": "secret"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    obj = payload["obj"]
    assert obj["db"] is True
    assert obj["degraded"] is False
    assert isinstance(obj["uptime"], float)
    assert isinstance(obj["threads"], int)
    assert set(obj["memory"]) == {"ram", "swap"}
    assert obj["daily_snapshot_failure"] is None
    assert obj["rollback_failures"] == {"uuid": {}, "registration": {}}


def test_health_returns_503_when_database_ping_fails(
    database: Database, flask_app: Flask, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> object:
        raise DatabaseError("down")

    _admin_api(database, flask_app)
    monkeypatch.setattr(database, "connection", fail)
    response = flask_app.test_client().get(
        "/sub/api/api/health", headers={"Authorization": "secret"},
    )
    assert response.status_code == 503
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["msg"] == "Database unavailable"
    assert payload["obj"]["db"] is False


def test_webapi_uses_configured_uri_prefix(database: Database, flask_app: Flask) -> None:
    subscription = make_subscription(database, app=flask_app, uri="custom", lang_cfg=_web_lang_cfg())
    database.create_user(
        username="alice", uuid=str(uuid.uuid4()), token="a" * 40,
        fingerprint="chrome", displayname="Alice",
    )
    database.set_auth_token("alice", "a" * 100)
    WebApi(
        app=flask_app,
        cfg=cast(ConfigLike, subscription_config(uri="custom")),
        sub=subscription,
        bw=make_watch(database, subscription),
    )
    client = flask_app.test_client()

    denied = client.get("/custom/panel")
    assert denied.status_code == 302
    assert denied.headers["Location"].endswith("/custom/auth")

    auth = client.get("/custom/auth")
    assert auth.status_code == 200
    assert b"__SUB_URI__" not in auth.data
    assert b"'/custom'" in auth.data
    assert b"/sub/" not in auth.data
    assert b'href="/custom/common.css?v=' in auth.data
    assert b'href="/custom/auth.css?v=' in auth.data

    client.set_cookie("auth_token", "a" * 100)
    panel = client.get("/custom/panel")
    assert panel.status_code == 200
    assert b"__SUB_URI__" not in panel.data
    assert b"window.BASE = '/custom'" in panel.data
    assert b'src="/custom/dashboard.js?v=' in panel.data
    history = client.get("/custom/history")
    assert history.status_code == 200
    assert b"__SUB_URI__" not in history.data
    assert b"window.BASE = '/custom'" in history.data
    assert b'src="/custom/history.js?v=' in history.data

    css = client.get("/custom/common.css")
    assert css.status_code == 200
    assert css.mimetype == "text/css"
    assert b"--accent:" in css.data
    assert "immutable" in css.headers["Cache-Control"]

    dashboard_js = client.get("/custom/dashboard.js")
    assert dashboard_js.status_code == 200
    assert dashboard_js.mimetype == "text/javascript"
    assert b"function loadStats" in dashboard_js.data
    history_js = client.get("/custom/history.js")
    assert history_js.status_code == 200
    assert history_js.mimetype == "text/javascript"
    assert b"function loadHistory" in history_js.data

    admin_js = client.get("/custom/admin/main.js")
    assert admin_js.status_code == 200
    assert admin_js.mimetype == "text/javascript"
    assert b"from './state.js?v=" in admin_js.data
    assert "immutable" in admin_js.headers["Cache-Control"]
    assert client.get("/custom/admin/not-a-module.js").status_code == 404
    charts_js = client.get("/custom/admin/charts.js")
    assert charts_js.status_code == 200
    assert b"from './charts/spec.js?v=" in charts_js.data
    assert b"from './charts/render.js?v=" in charts_js.data
    render_js = client.get("/custom/admin/charts/render.js")
    assert render_js.status_code == 200
    assert b"from './series.js?v=" in render_js.data
    assert b"from './theme.js?v=" in render_js.data


def test_page_lang_uses_query_then_cookie(database: Database, flask_app: Flask) -> None:
    subscription = make_subscription(database, app=flask_app, lang_cfg=_web_lang_cfg())
    WebApi(
        app=flask_app,
        cfg=cast(ConfigLike, subscription_config()),
        sub=subscription,
        bw=make_watch(database, subscription),
    )
    client = flask_app.test_client()

    russian = client.get("/sub/auth?lang=ru")
    assert russian.status_code == 200
    assert '"sign_in": "\\u0412\\u0445\\u043e\\u0434"' in russian.get_data(as_text=True)
    assert "lang=ru" in russian.headers.get("Set-Cookie", "")

    unknown = client.get("/sub/auth?lang=xx")
    assert unknown.status_code == 200
    assert '"sign_in": "Sign in"' in unknown.get_data(as_text=True)
    assert "lang=" not in unknown.headers.get("Set-Cookie", "")

    client.set_cookie("lang", "ru")
    from_cookie = client.get("/sub/auth")
    assert from_cookie.status_code == 200
    body = from_cookie.get_data(as_text=True)
    assert "window.LANG_CODE = 'ru'" in body
    assert '"sign_in": "\\u0412\\u0445\\u043e\\u0434"' in body


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


class _SharedRedis:
    """Enough of a Redis sorted set to run the rate-limit script in-process."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sets: dict[str, dict[str, float]] = {}
        self.closed = False

    def register_script(self, script: str) -> Callable[..., int]:
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
        ROUTES = []

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
        ROUTES = []

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
        ROUTES = []

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
        ROUTES = []

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
        ROUTES = []

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
            ROUTES = []

            @rate_limit(5)
            def endpoint(self: BaseApi) -> tuple[str, int]:
                return "ok", 200

        handler = object.__new__(Handler)
        with app_context(flask_app, "203.0.113.11"):
            assert handler.endpoint()[1] == 429
    finally:
        close_rate_limit()
    assert client.closed
