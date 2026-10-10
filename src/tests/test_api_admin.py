from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from flask import Flask
from flask.json.provider import DefaultJSONProvider
from helpers import config_mock, make_subscription, make_watch, subscription_config
from jinja2 import FileSystemLoader

from api import Api
from api.common import RES_DIR
from api.decorators.rate_limit import (  # pyright: ignore[reportPrivateUsage]
    _RateLimitScript,
    _replace_client,
    close_rate_limit,
)
from config import Config, LangConfig, LinesConfig
from errors import AppError, DatabaseError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from flask.testing import FlaskClient

    from db import Database

_LANG_PATH = Path(__file__).resolve().parents[2] / "lang.jsonc"


class _OrderedJSONProvider(DefaultJSONProvider):
    sort_keys = False


def _web_lang_cfg() -> Config[LangConfig]:
    return Config[LangConfig](path=_LANG_PATH, read_only=True, read_only_jsonc=True)


@pytest.fixture
def flask_app() -> Flask:
    app = Flask(__name__)
    app.json_provider_class = _OrderedJSONProvider
    app.json = _OrderedJSONProvider(app)
    # jinja_loader is a cached_property; assigning replaces the template lookup.
    app.jinja_env.loader = FileSystemLoader(str(RES_DIR))
    return app


class _OpenRedis:
    def register_script(self, script: str) -> _RateLimitScript:
        del script

        def run(keys: tuple[str, ...], args: tuple[float, float, str, int, int]) -> int:
            del keys, args
            return 1

        return run

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def open_rate_limit() -> Iterator[None]:
    _replace_client(_OpenRedis())
    yield
    close_rate_limit()


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
        cfg=config_mock(subscription_config(api_uri="api", api_token="secret")),
        audit_cfg=cast(LinesConfig, _Audit()),
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
        cfg=config_mock(subscription_config(
            api_uri=api_uri,
            api_token="secret",
            api_admin_ui_auth=list(api_admin_ui_auth),
        )),
        audit_cfg=cast(LinesConfig, _Audit()),
        sub=subscription,
        bw=make_watch(database, subscription),
    )


def test_admin_ui_requires_session(
    database: Database, flask_app: Flask,
) -> None:
    _admin_api(database, flask_app)
    denied = flask_app.test_client().get("/sub/admin")
    assert denied.status_code == 302
    assert denied.headers["Location"].endswith("/sub/admin/login")


def test_admin_login_page_omits_register_and_api(
    database: Database, flask_app: Flask,
) -> None:
    _admin_api(database, flask_app)
    login_page = flask_app.test_client().get("/sub/admin/login")
    assert login_page.status_code == 200
    assert b'id="loginForm"' in login_page.data
    assert b'id="tabRegister"' not in login_page.data
    assert b'id="registerForm"' not in login_page.data
    assert b"const SESSION = PANEL + '/session'" in login_page.data
    assert b"PANEL + '/token'" in login_page.data
    assert b"const API" not in login_page.data
    assert b"window.location.href = PANEL;" in login_page.data
    assert b"PANEL + '/'" not in login_page.data


def test_admin_session_rejects_invalid_login(
    database: Database, flask_app: Flask,
) -> None:
    _admin_api(database, flask_app)
    client = flask_app.test_client()
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


def test_admin_session_sets_httponly_cookie(
    database: Database, flask_app: Flask,
) -> None:
    _admin_api(database, flask_app)
    signed_in = flask_app.test_client().post(
        "/sub/admin/session", json={"username": "admin", "password": "panel-secret"},
    )
    assert signed_in.status_code == 200
    assert "admin_ui=" in signed_in.headers["Set-Cookie"]
    assert "Path=/sub/admin" in signed_in.headers["Set-Cookie"]
    assert "HttpOnly" in signed_in.headers["Set-Cookie"]
    session = database.admin_ui_session()
    assert session is not None
    assert len(session) == 100


def test_admin_page_uses_local_assets(
    database: Database, flask_app: Flask,
) -> None:
    _admin_api(database, flask_app)
    client = flask_app.test_client()
    client.post("/sub/admin/session", json={"username": "admin", "password": "panel-secret"})
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


def test_admin_relogin_invalidates_previous_cookie(
    database: Database, flask_app: Flask,
) -> None:
    _admin_api(database, flask_app)
    client = flask_app.test_client()
    client.post("/sub/admin/session", json={"username": "admin", "password": "panel-secret"})
    first = database.admin_ui_session()
    again = client.post(
        "/sub/admin/session", json={"username": "admin", "password": "panel-secret"},
    )
    assert again.status_code == 200
    second = database.admin_ui_session()
    assert first is not None
    assert second is not None
    assert second != first
    client.delete_cookie("admin_ui", path="/sub/admin")
    client.set_cookie("admin_ui", first, path="/sub/admin")
    assert client.get("/sub/admin").status_code == 302


def test_admin_logout_clears_session(
    database: Database, flask_app: Flask,
) -> None:
    _admin_api(database, flask_app)
    client = flask_app.test_client()
    client.post("/sub/admin/session", json={"username": "admin", "password": "panel-secret"})
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
        cfg=config_mock(subscription_config(
            uri="",
            api_uri="api",
            api_token="secret",
            api_admin_ui_auth=["admin", "panel-secret"],
        )),
        audit_cfg=cast(LinesConfig, _Audit()),
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


def test_admin_token_returns_secret_and_api_root(
    database: Database, flask_app: Flask,
) -> None:
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


def test_admin_token_api_root_omits_empty_api_uri(
    database: Database, flask_app: Flask,
) -> None:
    _admin_api(database, flask_app, api_uri="")
    client = flask_app.test_client()
    client.post("/sub/admin/session", json={"username": "admin", "password": "panel-secret"})
    response = client.get("/sub/admin/token")
    assert response.status_code == 200
    assert response.get_json()["obj"]["api_root"] == "/sub"


def _polling_api(
    database: Database, flask_app: Flask, monkeypatch: pytest.MonkeyPatch,
) -> FlaskClient:
    from helpers import FakePanel

    from custom_types import NetTrafficStats
    from sysutil import (
        AppMemory,
        ConnCount,
        LoadAverage,
        PollingSystemInfo,
        RamInfo,
        SwapInfo,
        SystemMemory,
    )

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
    monkeypatch.setattr("api.admin.state.SysUtil.polling_info", lambda: host)

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
        cfg=config_mock(subscription_config(api_uri="api", api_token="secret")),
        audit_cfg=cast(LinesConfig, _Audit()),
        sub=subscription,
        bw=make_watch(database, subscription),
    )
    return flask_app.test_client()


def test_polling_status_requires_admin_auth(
    database: Database, flask_app: Flask, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _polling_api(database, flask_app, monkeypatch)
    denied = client.get("/sub/api/api/state/polling")
    assert denied.status_code == 401


def test_polling_status_returns_limited_host_state(
    database: Database, flask_app: Flask, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _polling_api(database, flask_app, monkeypatch)
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


def test_polling_status_returns_limited_panel_state(
    database: Database, flask_app: Flask, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _polling_api(database, flask_app, monkeypatch)
    response = client.get("/sub/api/api/state/polling", headers={"Authorization": "secret"})
    edge = response.get_json()["obj"]["panels"]["edge"]
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


def test_polling_status_nulls_unknown_panel(
    database: Database, flask_app: Flask, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _polling_api(database, flask_app, monkeypatch)

    panels = client.get(
        "/sub/api/api/state/polling", headers={"Authorization": "secret"},
    ).get_json()["obj"]["panels"]
    assert set(panels) == {"edge", "down"}
    assert panels["down"] is None


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
