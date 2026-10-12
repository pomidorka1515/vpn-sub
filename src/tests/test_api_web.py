from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from argon2 import PasswordHasher
from async_api_helpers import api_app, config_snapshot, make_subscription, make_watch
from helpers import subscription_config

from api import AsyncWebApi
from config import Config, LangConfig

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from quart import Quart
    from quart.typing import TestClientProtocol

    from db import AsyncDatabase

pytestmark = pytest.mark.asyncio
LANG_PATH = Path(__file__).resolve().parents[2] / "lang.jsonc"


@pytest_asyncio.fixture
async def web_api(async_database: AsyncDatabase) -> AsyncIterator[tuple[Quart, AsyncDatabase]]:
    async for app in api_app():
        language = Config[LangConfig](path=LANG_PATH, read_only=True, read_only_jsonc=True)
        sub = await make_subscription(async_database, app=app, lang_cfg=language, uri="custom")
        await async_database.create_user(
            username="alice",
            uuid=str(uuid.uuid4()),
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            ext_username="alice-login",
            ext_password_hash=PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1).hash(
                "secret"
            ),
        )
        AsyncWebApi(
            app=app,
            cfg=config_snapshot(subscription_config(uri="custom")),
            sub=sub,
            bw=make_watch(async_database, sub),
            limiter=app.extensions["limiter"],
        )
        yield app, async_database
        language.close()


async def test_prefixed_pages_embed_uri(web_api: tuple[Quart, AsyncDatabase]) -> None:
    app, db = web_api
    client = app.test_client()
    denied = await client.get("/custom/panel")
    assert denied.status_code == 302
    assert denied.headers["Location"].endswith("/custom/auth")
    await db.set_auth_token("alice", "a" * 100)
    client.set_cookie("localhost", "auth_token", "a" * 100)
    for page, asset in (("auth", "auth.css"), ("panel", "dashboard.js"), ("history", "history.js")):
        response = await client.get(f"/custom/{page}")
        body = await response.get_data()
        assert isinstance(body, bytes)
        assert response.status_code == 200
        assert b"__SUB_URI__" not in body
        assert b"'/custom'" in body
        assert b"/sub/" not in body
        assert f"/custom/{asset}?v=".encode() in body
        assert b'href="/custom/common.css?v=' in body
        if page == "history":
            assert b"window.CHART_JS = '/custom/chart.umd.min.js?v=" in body
        assert b"fonts.googleapis" not in body
        assert b"fonts.gstatic" not in body
        assert b"jsdelivr" not in body


@pytest.mark.parametrize(
    ("path", "mimetype", "fragment"),
    [
        ("common.css", "text/css", b"url('/custom/fonts/outfit-latin.woff2')"),
        ("fonts/outfit-latin.woff2", "font/woff2", b"wOF2"),
        ("dashboard.js", "text/javascript", b"function loadStats"),
        ("history.js", "text/javascript", b"function loadHistory"),
        ("chart.umd.min.js", "text/javascript", b"Chart.js v4.4.1"),
        ("admin/main.js", "text/javascript", b"from './state.js?v="),
        ("admin/charts.js", "text/javascript", b"from './charts/spec.js?v="),
        ("admin/charts/render.js", "text/javascript", b"from './series.js?v="),
        ("admin/config.js", "text/javascript", b"from './config/config-main.js?v="),
        ("admin/json-editor.js", "text/javascript", b"codemirror-5.65.16.min.js"),
        ("vendor/codemirror-5.65.16.min.js", "text/javascript", b"CodeMirror"),
        ("vendor/codemirror-5.65.16.min.css", "text/css", b"CodeMirror"),
    ],
)
async def test_prefixed_static_assets(
    web_api: tuple[Quart, AsyncDatabase], path: str, mimetype: str, fragment: bytes
) -> None:
    response = await web_api[0].test_client().get(f"/custom/{path}")
    assert response.status_code == 200
    assert response.mimetype == mimetype
    body = await response.get_data()
    assert isinstance(body, bytes)
    assert fragment in body
    extras = {
        "common.css": (b"--accent:", b"url('/custom/fonts/jetbrains-mono-cyrillic.woff2')"),
        "admin/charts.js": (b"from './charts/render.js?v=",),
        "admin/charts/render.js": (b"from './theme.js?v=",),
        "admin/config.js": (b"function saveConfig", b"from './json-editor.js?v="),
    }
    for part in extras.get(path, ()):
        assert part in body
    if path == "common.css":
        for part in (b"fonts.googleapis", b"fonts.gstatic", b"__FONT_BASE__", b"/* __FONTS__ */"):
            assert part not in body
    if path.endswith(".woff2"):
        assert body[:4] == b"wOF2"
    assert "immutable" in response.headers["Cache-Control"]


@pytest.mark.parametrize(
    "module", ["state", "json", "controls", "main", "cards", "validation", "dialog"]
)
async def test_nested_config_modules(web_api: tuple[Quart, AsyncDatabase], module: str) -> None:
    response = await web_api[0].test_client().get(f"/custom/admin/config/config-{module}.js")
    assert response.status_code == 200
    assert response.mimetype == "text/javascript"
    assert "immutable" in response.headers["Cache-Control"]
    if module == "controls":
        body = await response.get_data()
        assert isinstance(body, bytes)
        assert b"from '../json-editor.js?v=" in body
        assert b"from './config-json.js?v=" in body


@pytest.mark.parametrize(
    "path", ["fonts/DejaVuSans.ttf", "fonts/../common.css", "admin/not-a-module.js"]
)
async def test_unknown_assets(web_api: tuple[Quart, AsyncDatabase], path: str) -> None:
    assert (await web_api[0].test_client().get(f"/custom/{path}")).status_code == 404


async def test_page_lang_uses_query_then_cookie(web_api: tuple[Quart, AsyncDatabase]) -> None:
    client = web_api[0].test_client()
    russian = await client.get("/custom/auth?lang=ru")
    assert russian.status_code == 200
    assert '"sign_in": "\\u0412\\u0445\\u043e\\u0434"' in await russian.get_data(as_text=True)
    assert "lang=ru" in russian.headers.get("Set-Cookie", "")
    unknown = await client.get("/custom/auth?lang=xx")
    assert unknown.status_code == 200
    assert '"sign_in": "Sign in"' in await unknown.get_data(as_text=True)
    assert "lang=" not in unknown.headers.get("Set-Cookie", "")
    client.set_cookie("localhost", "lang", "ru")
    response = await client.get("/custom/auth")
    assert response.status_code == 200
    assert "window.LANG_CODE = 'ru'" in await response.get_data(as_text=True)
    assert '"sign_in": "\\u0412\\u0445\\u043e\\u0434"' in await response.get_data(as_text=True)


async def login(client: TestClientProtocol) -> str:
    response = await client.post(
        "/custom/webapi/login", json={"username": "alice-login", "password": "secret"}
    )
    assert response.status_code == 200
    cookies = response.headers.getlist("Set-Cookie")
    assert any(cookie.startswith("token=;") for cookie in cookies)
    cookie = next(cookie for cookie in cookies if cookie.startswith("auth_token="))
    for value in ("Max-Age=2592000", "HttpOnly", "Secure", "SameSite=Lax"):
        assert value in cookie
    token = cookie.split("=", 1)[1].split(";", 1)[0]
    assert len(token) == 100
    assert token != "a" * 40
    assert "auth_token" not in (await response.get_json())["obj"]
    return token


async def test_login_uses_isolated_auth_token(web_api: tuple[Quart, AsyncDatabase]) -> None:
    client = web_api[0].test_client()
    response = await client.get("/custom/webapi/stats", headers={"Cookie": "token=" + "a" * 40})
    assert response.status_code == 401
    await login(client)
    response = await client.get("/custom/webapi/stats")
    assert response.status_code == 200
    assert (await response.get_json())["obj"]["token"] == "a" * 40


async def test_logout_invalidates_auth_token(web_api: tuple[Quart, AsyncDatabase]) -> None:
    app, db = web_api
    client = app.test_client()
    await login(client)
    response = await client.post("/custom/webapi/logout")
    assert response.status_code == 200
    assert response.headers["Set-Cookie"].count("auth_token=") == 1
    record = await db.get_user("alice")
    assert record is not None
    assert record["auth_token"] is None
    assert (await client.get("/custom/webapi/stats")).status_code == 401
