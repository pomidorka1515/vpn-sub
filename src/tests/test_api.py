from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, cast
from unittest import mock

import pytest
import uuid
import json
from argon2 import PasswordHasher
from flask import Flask
from pathlib import Path
from flask.json.provider import DefaultJSONProvider

from api import Api, BaseApi, WebApi, rate_limit
from api.config_patch import REQUIRED_KEYS, config_etag
from api.decorators.rate_limit import (  # pyright: ignore[reportPrivateUsage]
    _replace_client,
    close_rate_limit,
)
from api.common import RES_DIR
from config import Config, ConfigLike, JsonValue, LinesConfigLike
from db import Database
from errors import AppError, DatabaseError
from helpers import make_subscription, make_watch, subscription_config
from jinja2 import FileSystemLoader

_LANG_PATH = Path(__file__).resolve().parents[2] / "lang.jsonc"
_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "config.schema.json"
_EXAMPLE_PATH = Path(__file__).resolve().parents[2] / "docs" / "EXAMPLE.config.json"


class _OrderedJSONProvider(DefaultJSONProvider):
    sort_keys = False


def _web_lang_cfg() -> ConfigLike:
    return cast(ConfigLike, Config(path=_LANG_PATH, read_only=True, read_only_jsonc=True))


@pytest.fixture
def flask_app() -> Flask:
    app = Flask(__name__)
    app.json_provider_class = _OrderedJSONProvider
    app.json = _OrderedJSONProvider(app)
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

    def add_users(
        username: str,
        _called_internally: bool = False,
        *,
        known_clients: object = None,
    ) -> None:
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
    assert b"fonts.googleapis" not in response.data
    assert b"fonts.gstatic" not in response.data
    assert b'type="module"' in response.data
    assert b'src="/sub/admin/main.js?v=' in response.data
    assert b'id="btnLogout"' in response.data
    assert b"window.CHART_JS = '/sub/chart.umd.min.js?v=" in response.data
    assert b"jsdelivr" not in response.data

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
    from custom_types import NetTrafficStats
    from sysutil import (
        AppMemory, ConnCount, LoadAverage,
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
    assert b"fonts.googleapis" not in auth.data
    assert b"fonts.gstatic" not in auth.data

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
    assert b"window.CHART_JS = '/custom/chart.umd.min.js?v=" in history.data
    assert b"jsdelivr" not in history.data

    css = client.get("/custom/common.css")
    assert css.status_code == 200
    assert css.mimetype == "text/css"
    assert b"--accent:" in css.data
    assert b"fonts.googleapis" not in css.data
    assert b"fonts.gstatic" not in css.data
    assert b"url('/custom/fonts/outfit-latin.woff2')" in css.data
    assert b"url('/custom/fonts/jetbrains-mono-cyrillic.woff2')" in css.data
    assert b"__FONT_BASE__" not in css.data
    assert b"/* __FONTS__ */" not in css.data
    assert "immutable" in css.headers["Cache-Control"]

    font = client.get("/custom/fonts/outfit-latin.woff2")
    assert font.status_code == 200
    assert font.mimetype == "font/woff2"
    assert font.data[:4] == b"wOF2"
    assert "immutable" in font.headers["Cache-Control"]
    assert client.get("/custom/fonts/DejaVuSans.ttf").status_code == 404
    assert client.get("/custom/fonts/../common.css").status_code == 404

    dashboard_js = client.get("/custom/dashboard.js")
    assert dashboard_js.status_code == 200
    assert dashboard_js.mimetype == "text/javascript"
    assert b"function loadStats" in dashboard_js.data
    history_js = client.get("/custom/history.js")
    assert history_js.status_code == 200
    assert history_js.mimetype == "text/javascript"
    assert b"function loadHistory" in history_js.data
    chart_js = client.get("/custom/chart.umd.min.js")
    assert chart_js.status_code == 200
    assert chart_js.mimetype == "text/javascript"
    assert b"Chart.js v4.4.1" in chart_js.data
    assert "immutable" in chart_js.headers["Cache-Control"]

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


class _RecordingAudit:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def append(self, record: object) -> None:
        self.records.append(cast(dict[str, object], record))


def _valid_config() -> dict[str, Any]:
    data = json.loads(_EXAMPLE_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    data["$schema"] = "../config.schema.json"
    data["api_token"] = "secret-token"
    data["api_admin_ui_auth"] = ["admin", "panel-secret"]
    data["publicbot"] = {"token": "123:public"}
    data["sub_name"] = "before"
    data["funny_strings"] = ["keep-me"]
    return data


def _bad_node_profiles() -> dict[str, Any]:
    profile = dict(_valid_config()["profiles"]["profile1"])
    profile["node"] = "missing"
    return {"profile1": profile}


def _config_api(tmp_path: Path, flask_app: Flask) -> tuple[Config, Path, _RecordingAudit]:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_valid_config(), indent=4) + "\n", encoding="utf-8")
    cfg = Config(
        path=path,
        schema_path=_SCHEMA_PATH,
        backup_dir=tmp_path / "backup",
        sync_mode="none",
        start_backup=False,
    )
    audit = _RecordingAudit()
    database = Database(path=tmp_path / "state.sqlite3", timeout=2)
    subscription = make_subscription(
        database,
        app=flask_app,
        audit_cfg=cast(LinesConfigLike, audit),
    )
    Api(
        app=flask_app,
        cfg=cast(ConfigLike, cfg),
        audit_cfg=cast(LinesConfigLike, audit),
        sub=subscription,
        bw=make_watch(subscription.res.db, subscription),
    )
    database.close()
    return cfg, path, audit


def _schema_required() -> list[str]:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    required = schema["required"]
    assert isinstance(required, list)
    return [item for item in required if isinstance(item, str)]


def test_required_keys_match_schema() -> None:
    assert list(REQUIRED_KEYS) == _schema_required()


def test_config_get_requires_token(tmp_path: Path, flask_app: Flask) -> None:
    cfg, _path, _audit = _config_api(tmp_path, flask_app)
    try:
        denied = flask_app.test_client().get("/sub/api/config/get")
        assert denied.status_code == 401
        denied_set = flask_app.test_client().post("/sub/api/config/set", json={"base": "x", "values": {}})
        assert denied_set.status_code == 401
    finally:
        cfg.close()


def test_config_get_returns_file_and_stable_etag(tmp_path: Path, flask_app: Flask) -> None:
    cfg, path, _audit = _config_api(tmp_path, flask_app)
    try:
        client = flask_app.test_client()
        headers = {"Authorization": "secret-token"}
        first = client.get("/sub/api/config/get", headers=headers)
        second = client.get("/sub/api/config/get", headers=headers)
        assert first.status_code == 200
        payload = first.get_json()
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert payload["obj"] == on_disk
        body = first.data.decode("utf-8")
        assert body.count("\n") == 1 and body.endswith("\n")
        assert ": " not in body and ", " not in body
        assert first.headers["ETag"] == f'"{config_etag(cast(dict[str, JsonValue], on_disk))}"'
        assert first.headers["Cache-Control"] == "no-store"
        obj_at = body.index('"obj":')
        schema_at = body.index('"$schema":')
        assert obj_at < schema_at
        assert second.headers["ETag"] == first.headers["ETag"]
        assert payload["obj"]["api_token"] == "secret-token"
    finally:
        cfg.close()


def _set(
    flask_app: Flask,
    base: str,
    values: dict[str, Any],
) -> Any:
    return flask_app.test_client().post(
        "/sub/api/config/set",
        json={"base": base, "values": values},
        headers={"Authorization": "secret-token"},
    )


def test_config_set_one_key_keeps_indent_and_other_keys(tmp_path: Path, flask_app: Flask) -> None:
    cfg, path, audit = _config_api(tmp_path, flask_app)
    try:
        before = path.read_bytes()
        original = json.loads(before)
        base = config_etag(cast(dict[str, JsonValue], original))
        response = _set(flask_app, base, {"sub_name": "after"})
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["msg"] == "Updated"
        assert payload["obj"]["restart"] == []
        written = path.read_text(encoding="utf-8")
        assert written.endswith("\n")
        assert '\n    "sub_name": "after"' in written
        updated = json.loads(written)
        original["sub_name"] = "after"
        assert updated == original
        assert payload["obj"]["base"] == config_etag(cast(dict[str, JsonValue], updated))
        assert audit.records[-1]["action"] == "config_update"
        info = audit.records[-1]["info"]
        assert isinstance(info, dict)
        assert info["keys"] == ["sub_name"]
        assert "after" not in json.dumps(info)
    finally:
        cfg.close()


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"nope": 1}, "Unknown key"),
        ({"panel_alert_cooldown": "soon"}, "Schema validation"),
        ({"3xui": {}}, "3xui must contain"),
        ({"$schema": "other.json"}, "$schema"),
    ],
)
def test_config_set_rejects_without_writing(
    tmp_path: Path,
    flask_app: Flask,
    values: dict[str, Any],
    message: str,
) -> None:
    cfg, path, audit = _config_api(tmp_path, flask_app)
    try:
        before = path.read_bytes()
        base = config_etag(cast(dict[str, JsonValue], json.loads(before)))
        response = _set(flask_app, base, values)
        assert response.status_code == 400
        assert message in response.get_json()["msg"]
        assert path.read_bytes() == before
        assert audit.records == []
        assert not (tmp_path / "backup").exists()
    finally:
        cfg.close()


def test_config_set_rejects_unknown_profile_node(tmp_path: Path, flask_app: Flask) -> None:
    cfg, path, audit = _config_api(tmp_path, flask_app)
    try:
        before = path.read_bytes()
        base = config_etag(cast(dict[str, JsonValue], json.loads(before)))
        response = _set(flask_app, base, {"profiles": _bad_node_profiles()})
        assert response.status_code == 400
        assert "not a key of nodes" in response.get_json()["msg"]
        assert path.read_bytes() == before
        assert audit.records == []
    finally:
        cfg.close()


def test_config_set_stale_base_is_409(tmp_path: Path, flask_app: Flask) -> None:
    cfg, path, _audit = _config_api(tmp_path, flask_app)
    try:
        before = path.read_bytes()
        current = config_etag(cast(dict[str, JsonValue], json.loads(before)))
        response = _set(flask_app, "0" * 64, {"sub_name": "after"})
        assert response.status_code == 409
        payload = response.get_json()
        assert payload["msg"] == "Config changed since it was loaded"
        assert payload["obj"]["base"] == current
        assert path.read_bytes() == before
    finally:
        cfg.close()


def test_config_set_rejects_a_non_object_body(tmp_path: Path, flask_app: Flask) -> None:
    cfg, path, _audit = _config_api(tmp_path, flask_app)
    try:
        before = path.read_bytes()
        client = flask_app.test_client()
        headers = {"Authorization": "secret-token"}
        missing = client.post("/sub/api/config/set", json={"values": {}}, headers=headers)
        assert missing.status_code == 400
        listed = client.post("/sub/api/config/set", json=["nope"], headers=headers)
        assert listed.status_code == 400
        assert path.read_bytes() == before
        quoted = _set(
            flask_app,
            f'"{config_etag(cast(dict[str, JsonValue], json.loads(before)))}"',
            {"sub_name": "after"},
        )
        assert quoted.status_code == 200
        assert json.loads(path.read_text(encoding="utf-8"))["sub_name"] == "after"
    finally:
        cfg.close()


def test_config_set_null_deletes_optional_and_rejects_required(
    tmp_path: Path,
    flask_app: Flask,
) -> None:
    cfg, path, _audit = _config_api(tmp_path, flask_app)
    try:
        before = path.read_bytes()
        base = config_etag(cast(dict[str, JsonValue], json.loads(before)))
        rejected = _set(flask_app, base, {"domain": None})
        assert rejected.status_code == 400
        assert "required" in rejected.get_json()["msg"]
        assert path.read_bytes() == before

        removed = _set(flask_app, base, {"funny_strings": None, "domain": "https://example.test"})
        assert removed.status_code == 200
        updated = json.loads(path.read_text(encoding="utf-8"))
        assert "funny_strings" not in updated
        assert updated["domain"] == "https://example.test"
        assert updated["sub_name"] == "before"
    finally:
        cfg.close()


def test_config_set_unchanged_does_not_touch_mtime(tmp_path: Path, flask_app: Flask) -> None:
    cfg, path, audit = _config_api(tmp_path, flask_app)
    try:
        before = path.read_bytes()
        mtime = path.stat().st_mtime_ns
        base = config_etag(cast(dict[str, JsonValue], json.loads(before)))
        response = _set(flask_app, base, {"sub_name": "before"})
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["msg"] == "Unchanged"
        assert payload["obj"]["restart"] == []
        assert path.read_bytes() == before
        assert path.stat().st_mtime_ns == mtime
        assert audit.records == []
    finally:
        cfg.close()


def test_config_set_restart_lists_only_captured_keys(tmp_path: Path, flask_app: Flask) -> None:
    cfg, path, _audit = _config_api(tmp_path, flask_app)
    try:
        original = json.loads(path.read_text(encoding="utf-8"))
        base = config_etag(cast(dict[str, JsonValue], original))
        domain = _set(flask_app, base, {"domain": "https://other.test"})
        assert domain.status_code == 200
        assert domain.get_json()["obj"]["restart"] == []

        panels = json.loads(path.read_text(encoding="utf-8"))["3xui"]
        panels["local_panel"]["name"] = "renamed"
        changed = _set(flask_app, domain.get_json()["obj"]["base"], {"3xui": panels})
        assert changed.status_code == 200
        assert changed.get_json()["obj"]["restart"] == ["3xui"]
    finally:
        cfg.close()


def test_config_set_failed_commit_does_not_back_up_or_audit(
    tmp_path: Path,
    flask_app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, path, audit = _config_api(tmp_path, flask_app)
    try:
        before = path.read_bytes()
        base = config_etag(cast(dict[str, JsonValue], json.loads(before)))

        def fail_write(data: dict[str, JsonValue]) -> None:
            del data
            raise OSError("disk full")

        monkeypatch.setattr(cfg, "_atomic_write", fail_write)
        response = _set(flask_app, base, {"sub_name": "after"})
        assert response.status_code == 500
        assert path.read_bytes() == before
        assert audit.records == []
        assert not (tmp_path / "backup").exists()
    finally:
        cfg.close()
