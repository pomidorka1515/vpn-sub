from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from argon2 import PasswordHasher
from flask import Flask
from flask.json.provider import DefaultJSONProvider
from helpers import config_mock, make_subscription, make_watch, subscription_config
from jinja2 import FileSystemLoader

from api import WebApi
from api.common import RES_DIR
from api.decorators.rate_limit import (  # pyright: ignore[reportPrivateUsage]
    _RateLimitScript,
    _replace_client,
    close_rate_limit,
)
from config import Config, LangConfig

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
        app=flask_app, cfg=config_mock(subscription_config()),
        sub=subscription, bw=watcher,
    )
    return flask_app, database


def _prefixed_client(database: Database, flask_app: Flask) -> FlaskClient:
    subscription = make_subscription(database, app=flask_app, uri="custom", lang_cfg=_web_lang_cfg())
    database.create_user(
        username="alice", uuid=str(uuid.uuid4()), token="a" * 40,
        fingerprint="chrome", displayname="Alice",
    )
    database.set_auth_token("alice", "a" * 100)
    WebApi(
        app=flask_app,
        cfg=config_mock(subscription_config(uri="custom")),
        sub=subscription,
        bw=make_watch(database, subscription),
    )
    return flask_app.test_client()


def test_prefixed_panel_redirects_unauthenticated(database: Database, flask_app: Flask) -> None:
    client = _prefixed_client(database, flask_app)

    denied = client.get("/custom/panel")
    assert denied.status_code == 302
    assert denied.headers["Location"].endswith("/custom/auth")


def test_prefixed_pages_embed_uri(database: Database, flask_app: Flask) -> None:
    client = _prefixed_client(database, flask_app)

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


def test_prefixed_static_assets(database: Database, flask_app: Flask) -> None:
    client = _prefixed_client(database, flask_app)

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


def test_prefixed_admin_modules(database: Database, flask_app: Flask) -> None:
    client = _prefixed_client(database, flask_app)

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
        cfg=config_mock(subscription_config()),
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


def test_login_uses_isolated_auth_token(
    web_api: tuple[Flask, Database],
) -> None:
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


def test_logout_invalidates_auth_token(
    web_api: tuple[Flask, Database],
) -> None:
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
