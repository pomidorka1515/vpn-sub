from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from argon2 import PasswordHasher
from async_api_helpers import api_app, config_snapshot, make_subscription, make_watch
from helpers import subscription_config

from api import AsyncWebApi

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from quart import Quart
    from quart.typing import TestClientProtocol

    from config import Config, LangConfig
    from db import Database

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def web_api(
    database: Database,
    language: Config[LangConfig],
) -> AsyncIterator[tuple[Quart, Database]]:
    async for app in api_app():
        sub = await make_subscription(database, app=app, lang_cfg=language, uri="custom")
        await database.create_user(
            username="alice",
            uuid=str(uuid.uuid4()),
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            ext_username="alice-login",
            ext_password_hash=PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1).hash(
                "secret",
            ),
        )
        AsyncWebApi(
            app=app,
            cfg=config_snapshot(subscription_config(uri="custom")),
            sub=sub,
            bw=make_watch(database, sub),
            limiter=app.extensions["limiter"],
        )
        yield app, database


@pytest.mark.parametrize("field", ["username", "password", "code", "name"])
@pytest.mark.parametrize("invalid", [None, 42, [], {}])
async def test_register_rejects_invalid_required_fields(
    web_api: tuple[Quart, Database],
    field: str,
    invalid: object,
) -> None:
    app, db = web_api
    body: dict[str, object] = {
        "username": "new-user",
        "password": "secret",
        "code": "invite",
        "name": "New User",
    }
    body[field] = invalid
    response = await app.test_client().post("/custom/webapi/register", json=body)
    assert response.status_code == 400
    data = await response.get_json()
    assert data["success"] is False
    assert data["msg"] == f"Field '{field}' must be str, got {type(invalid).__name__}"
    assert await db.ext_to_user("new-user") is None


@pytest.mark.parametrize(
    "field", ["name", "fingerprint", "username", "password", "current_password"]
)
@pytest.mark.parametrize("value", [42, 0, 1.5, True, False, [], {}, ["chrome"]])
async def test_settings_reject_non_string_fields(
    web_api: tuple[Quart, Database],
    field: str,
    value: object,
) -> None:
    app, db = web_api
    await db.set_auth_token("alice", "a" * 100)
    client = app.test_client()
    client.set_cookie("localhost", "auth_token", "a" * 100)
    before = await db.get_user("alice")
    response = await client.post(
        "/custom/webapi/settings",
        json={"name": "Updated", field: value},
    )
    assert response.status_code == 400
    assert (await response.get_json())["success"] is False
    assert await db.get_user("alice") == before


@pytest.mark.parametrize(
    "fields",
    [{}, dict.fromkeys(["name", "fingerprint", "username", "password", "current_password"])],
)
async def test_settings_allow_omitted_and_null_fields(
    web_api: tuple[Quart, Database],
    fields: dict[str, None],
) -> None:
    app, db = web_api
    await db.set_auth_token("alice", "a" * 100)
    client = app.test_client()
    client.set_cookie("localhost", "auth_token", "a" * 100)
    before = await db.get_user("alice")
    response = await client.post("/custom/webapi/settings", json=fields)
    assert response.status_code == 200
    assert await db.get_user("alice") == before


async def test_expired_web_cookie_cannot_be_replayed(web_api: tuple[Quart, Database]) -> None:
    app, db = web_api
    await db.set_auth_token("alice", "a" * 100)
    async with db.transaction(immediate=True) as conn:
        await conn.execute("UPDATE users SET auth_token_expires_at = 1 WHERE username = 'alice'")
    client = app.test_client()
    client.set_cookie("localhost", "auth_token", "a" * 100)
    assert (await client.get("/custom/panel")).status_code == 302
    assert (await client.post("/custom/webapi/logout")).status_code == 401


@pytest.mark.parametrize(
    ("username", "valid", "taken", "sanitized"),
    [
        ("!!!", False, False, ""),
        ("alice-login", True, True, "alice-login"),
        ("alice!", False, False, "alice"),
        ("a" * 33, False, False, "a" * 32),
    ],
)
async def test_validate_username_sanitization(
    web_api: tuple[Quart, Database],
    username: str,
    *,
    valid: bool,
    taken: bool,
    sanitized: str,
) -> None:
    response = (
        await web_api[0]
        .test_client()
        .get(
            "/custom/webapi/validate",
            query_string={"username": username},
        )
    )
    assert response.status_code == 200
    data = await response.get_json()
    assert data["obj"] == {
        "MAX_LENGTH": 32,
        "valid": valid,
        "taken": taken,
        "sanitized": sanitized,
    }


async def test_prefixed_pages_embed_uri(web_api: tuple[Quart, Database]) -> None:
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
    web_api: tuple[Quart, Database],
    path: str,
    mimetype: str,
    fragment: bytes,
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
    "module",
    ["state", "json", "controls", "main", "cards", "validation", "dialog"],
)
async def test_nested_config_modules(web_api: tuple[Quart, Database], module: str) -> None:
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
    "path",
    [
        "fonts/DejaVuSans.ttf",
        "fonts/../common.css",
        "admin/not-a-module.js",
        "admin/config/not-a-module.js",
        "admin/../dashboard.js",
    ],
)
async def test_unknown_assets(web_api: tuple[Quart, Database], path: str) -> None:
    assert (await web_api[0].test_client().get(f"/custom/{path}")).status_code == 404


@pytest.mark.parametrize("path", ["/missing", "/custom/fonts/missing.woff2"])
@pytest.mark.parametrize("browser", [True, False])
async def test_not_found_uses_html_only_for_browsers(
    web_api: tuple[Quart, Database],
    path: str,
    *,
    browser: bool,
) -> None:
    agent = "Mozilla/5.0 Chrome/130.0 Safari/537.36" if browser else "curl/8.0"
    response = await web_api[0].test_client().get(path, headers={"User-Agent": agent})
    assert response.status_code == 404
    if browser:
        assert response.mimetype == "text/html"
        body = await response.get_data(as_text=True)
        assert "Page not found" in body
        assert "<script src=" not in body
        assert '<link rel="stylesheet"' not in body
    else:
        assert response.mimetype == "application/json"
        assert (await response.get_json())["success"] is False


async def test_page_lang_uses_query_then_cookie(web_api: tuple[Quart, Database]) -> None:
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
        "/custom/webapi/login",
        json={"username": "alice-login", "password": "secret"},
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


async def test_login_uses_isolated_auth_token(web_api: tuple[Quart, Database]) -> None:
    client = web_api[0].test_client()
    response = await client.get("/custom/webapi/stats", headers={"Cookie": "token=" + "a" * 40})
    assert response.status_code == 401
    await login(client)
    response = await client.get("/custom/webapi/stats")
    assert response.status_code == 200
    assert (await response.get_json())["obj"]["token"] == "a" * 40


async def test_logout_invalidates_auth_token(web_api: tuple[Quart, Database]) -> None:
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
