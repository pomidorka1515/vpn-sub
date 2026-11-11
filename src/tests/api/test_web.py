from __future__ import annotations

import json
import re
import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from argon2 import PasswordHasher

from api import AsyncApi, AsyncWebApi
from config.store import AsyncLinesConfig
from fonts import FONT_FILES, font_version
from paths import frontend_root
from tests.support.api import api_app, config_snapshot, make_subscription, make_watch
from tests.support.config import subscription_config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from quart import Quart
    from quart.typing import TestClientProtocol

    from config import Config, LangConfig
    from db import Database

pytestmark = pytest.mark.asyncio

SESSION_ASSETS = {
    "dashboard.js": "assets/dashboard.js",
    "history.js": "assets/history.js",
    "chart.umd.min.js": "vendor/chart.umd.min.js",
    "dashboard.css": "assets/dashboard.css",
    "history.css": "assets/history.css",
}


@pytest.mark.parametrize("query", ["", "?token=", "?token=invalid", "?token=" + "x" * 75])
async def test_password_reset_invalid_token_is_404(
    web_api: tuple[Quart, Database], query: str
) -> None:
    client = web_api[0].test_client()
    response = await client.get("/custom/reset" + query, headers={"User-Agent": "Mozilla/5.0"})
    assert response.status_code == 404
    assert "resetForm" not in await response.get_data(as_text=True)
    response = await client.post("/custom/webapi/password-reset" + query, json={"password": "new"})
    assert response.status_code == 404


async def test_password_reset_without_authentication(web_api: tuple[Quart, Database]) -> None:
    app, db = web_api
    sub = app.extensions["subscriptions"][0]
    link = await sub.account_svc.issue_password_reset("alice")
    query = "?" + link.split("?", 1)[1]
    client = app.test_client()
    response = await client.get("/custom/reset" + query)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "resetForm" in await response.get_data(as_text=True)
    assert (await client.post("/custom/webapi/password-reset" + query, json={})).status_code == 400
    response = await client.post("/custom/webapi/password-reset" + query, json={"password": "new"})
    assert response.status_code == 200
    assert await sub.password_svc.validate_credentials("alice-login", "new") == "alice"
    assert await db.ext_to_user("alice-login") == "alice"
    assert (await client.get("/custom/reset" + query)).status_code == 404


async def test_admin_password_reset_requires_admin_and_issues_public_link(
    web_api: tuple[Quart, Database],
) -> None:
    app, db = web_api
    sub = app.extensions["subscriptions"][0]
    AsyncApi(
        app=app,
        cfg=config_snapshot(subscription_config(uri="custom")),
        sub=sub,
        bw=make_watch(db, sub),
        limiter=app.extensions["limiter"],
        audit_cfg=MagicMock(spec=AsyncLinesConfig),
    )
    client = app.test_client()
    path = "/custom/api/user/password-reset"
    assert (await client.post(path, json={"user": "alice"})).status_code == 401
    await db.set_auth_token("alice", "a" * 100)
    client.set_cookie("localhost", "auth_token", "a" * 100)
    assert (await client.post(path, json={"user": "alice"})).status_code == 401
    response = await client.post(path, json={"user": "alice"}, headers={"Authorization": "secret"})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    link = (await response.get_json())["obj"]["link"]
    assert re.fullmatch(r"https://example.test/custom/reset\?token=[A-Za-z0-9_-]{75}", link)
    # Issuing the link neither changes the password nor signs out the user.
    assert await sub.password_svc.validate_credentials("alice-login", "secret") == "alice"
    assert await db.auth_token_to_user("a" * 100) == "alice"


@pytest_asyncio.fixture
async def web_api(
    database: Database,
    language: Config[LangConfig],
) -> AsyncIterator[tuple[Quart, Database]]:
    async with api_app() as app:
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


@pytest.mark.parametrize("agent", ["Mozilla/5.0 Chrome/130.0 Safari/537.36", "curl/8.0"])
async def test_invalid_registration_invitation_preserves_json(
    web_api: tuple[Quart, Database],
    agent: str,
) -> None:
    response = (
        await web_api[0]
        .test_client()
        .post(
            "/custom/webapi/register",
            headers={"User-Agent": agent},
            json={"username": "new-user", "password": "secret", "code": "missing", "name": "New"},
        )
    )
    assert response.status_code == 404
    assert response.mimetype == "application/json"
    data = await response.get_json()
    assert data["success"] is False
    assert data["msg"] == "Invalid code"


async def test_registration_rejects_changed_username_then_logs_in_with_exact_input(
    web_api: tuple[Quart, Database],
) -> None:
    app, db = web_api
    await db.add_code("invite", "register", uses=1)
    client = app.test_client()
    body = {
        "username": "a!lice",
        "password": "secret",
        "code": "invite",
        "name": "Alice",
    }
    response = await client.post("/custom/webapi/register", json=body)
    assert response.status_code == 400
    assert (await response.get_json())["success"] is False
    assert await db.ext_to_user("alice") is None
    assert await db.ext_to_user("a!lice") is None
    invite = await db.get_code("invite")
    assert invite is not None
    assert invite["uses"] == 1

    body["username"] = "Alice_01-test"
    response = await client.post("/custom/webapi/register", json=body)
    assert response.status_code == 201
    internal = (await response.get_json())["obj"]["username"]
    assert await db.ext_to_user(body["username"]) == internal
    assert await db.get_code("invite") is None
    response = await client.post(
        "/custom/webapi/login",
        json={"username": body["username"], "password": body["password"]},
    )
    assert response.status_code == 200
    assert (await response.get_json())["success"] is True
    assert "auth_token=" in response.headers["Set-Cookie"]


@pytest.mark.parametrize(
    "field",
    ["name", "fingerprint", "username", "password", "current_password"],
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
        match = re.search(
            rb'<script id="frontend-bootstrap" type="application/json">(.*?)</script>',
            body,
            re.DOTALL,
        )
        assert match is not None
        bootstrap = json.loads(match[1])
        assert bootstrap["prefix"] == "/custom"
        assert bootstrap["language"] == "en"
        assert bootstrap["translations"]
        assert bootstrap["fallback"] == bootstrap["translations"]
        assert bootstrap["page"]["debounceMs"] == 1000
        assert body.index(b'id="frontend-bootstrap"') < body.index(b'src="/custom/common.js')
        assert body.index(b'src="/custom/common.js') < body.index(b'<link rel="stylesheet"')
        assert b"/sub/" not in body
        assert f"/custom/{asset}?v=".encode() in body
        assert b'href="/custom/common.css?v=' in body
        if page == "history":
            assert bootstrap["page"]["chartUrl"].startswith("/custom/chart.umd.min.js?v=")
        assert b"fonts.googleapis" not in body
        assert b"fonts.gstatic" not in body
        assert b"jsdelivr" not in body


@pytest.mark.parametrize(
    ("path", "mimetype", "fragment"),
    [
        ("common.css", "text/css", b"url('/custom/fonts/outfit-latin.woff2?v="),
        ("fonts/outfit-latin.woff2", "font/woff2", b"wOF2"),
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
        "common.css": (b"--accent:",),
    }
    for part in extras.get(path, ()):
        assert part in body
    if path == "common.css":
        for name in FONT_FILES:
            url = f"/custom/fonts/{name}?v={font_version()}"
            assert f"url('{url}')".encode() in body
            font = await web_api[0].test_client().get(url)
            assert font.status_code == 200
            assert (await font.get_data())[:4] == b"wOF2"
            assert "immutable" in font.headers["Cache-Control"]
        for part in (
            b"fonts.googleapis",
            b"fonts.gstatic",
            b"__FONT_BASE__",
            b"__FONT_VERSION__",
            b"/* __FONTS__ */",
        ):
            assert part not in body
    if path.endswith(".woff2"):
        assert body[:4] == b"wOF2"
    assert "immutable" in response.headers["Cache-Control"]


@pytest.mark.parametrize("path", SESSION_ASSETS)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("cookie", [None, "invalid", "x" * 100, "a" * 40])
async def test_dashboard_assets_require_user_session(
    web_api: tuple[Quart, Database], path: str, method: str, cookie: str | None
) -> None:
    client = web_api[0].test_client()
    if cookie is not None:
        client.set_cookie("localhost", "auth_token", cookie)
    client.set_cookie("localhost", "token", "a" * 40)
    client.set_cookie("localhost", "admin_ui", "x" * 100)
    request_asset = client.get if method == "GET" else client.head
    response = await request_asset(
        f"/custom/{path}?v=test",
        headers={"Authorization": "secret", "If-None-Match": "*"},
    )
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    assert response.mimetype != "text/javascript"
    assert response.mimetype != "text/css"


@pytest.mark.parametrize(("path", "resource"), SESSION_ASSETS.items())
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_dashboard_assets_with_session_are_not_stored(
    web_api: tuple[Quart, Database], path: str, resource: str, method: str
) -> None:
    client = web_api[0].test_client()
    await login(client)
    request_asset = client.get if method == "GET" else client.head
    response = await request_asset(f"/custom/{path}?v=test", headers={"If-None-Match": "*"})
    assert response.status_code == 200
    assert response.mimetype == ("text/css" if path.endswith(".css") else "text/javascript")
    assert response.headers["Cache-Control"] == "no-store"
    assert "ETag" not in response.headers
    body = await response.get_data()
    assert isinstance(body, bytes)
    if method == "GET":
        assert body == (frontend_root() / resource).read_bytes()
        if path in {"dashboard.js", "history.js"}:
            assert b"sourceMappingURL" not in body
            assert b"// src/" not in body
            assert b"\n" not in body


@pytest.mark.parametrize("path", SESSION_ASSETS)
@pytest.mark.parametrize("revoke", ["logout", "relogin", "expire"])
async def test_dashboard_assets_reject_revoked_sessions(
    web_api: tuple[Quart, Database], path: str, revoke: str
) -> None:
    app, db = web_api
    client = app.test_client()
    token = await login(client)
    assert (await client.get(f"/custom/{path}")).status_code == 200
    if revoke == "logout":
        assert (await client.post("/custom/webapi/logout")).status_code == 200
    elif revoke == "relogin":
        await login(app.test_client())
    else:
        async with db.transaction(immediate=True) as conn:
            await conn.execute(
                "UPDATE users SET auth_token_expires_at = 1 WHERE username = 'alice'"
            )
    client.set_cookie("localhost", "auth_token", token)
    response = await client.get(f"/custom/{path}", headers={"If-None-Match": "*"})
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path", SESSION_ASSETS)
async def test_dashboard_asset_auth_storage_errors_are_not_stored(
    web_api: tuple[Quart, Database], monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    app, _ = web_api
    client = app.test_client()
    await login(client)
    sub = app.extensions["subscriptions"][0]
    monkeypatch.setattr(sub.user_svc, "auth_token_to_user", AsyncMock(side_effect=RuntimeError()))
    response = await client.get(f"/custom/{path}")
    assert response.status_code == 500
    assert response.headers["Cache-Control"] == "no-store"


async def test_admin_bundle_is_not_a_public_static_asset(web_api: tuple[Quart, Database]) -> None:
    assert (await web_api[0].test_client().get("/custom/admin/main.js")).status_code == 404


async def test_user_and_admin_asset_sessions_are_separate(web_api: tuple[Quart, Database]) -> None:
    app, db = web_api
    sub = app.extensions["subscriptions"][0]
    AsyncApi(
        app=app,
        cfg=config_snapshot(
            subscription_config(uri="custom", api_admin_ui_auth=["admin", "panel-secret"])
        ),
        sub=sub,
        bw=make_watch(db, sub),
        limiter=app.extensions["limiter"],
        audit_cfg=MagicMock(spec=AsyncLinesConfig),
    )
    user = app.test_client()
    await login(user)
    admin = app.test_client()
    response = await admin.post(
        "/custom/admin/session", json={"username": "admin", "password": "panel-secret"}
    )
    assert response.status_code == 200
    for path in SESSION_ASSETS:
        response = await admin.get(f"/custom/{path}")
        assert response.status_code == 401
        assert response.headers["Cache-Control"] == "no-store"
    for path in (
        "main.js",
        "chart.umd.min.js",
        "vendor/codemirror-5.65.16.min.js",
        "main.css",
        "charts.css",
        "vendor/codemirror-5.65.16.min.css",
    ):
        response = await user.get(f"/custom/admin/{path}")
        assert response.status_code == 401
        assert response.headers["Cache-Control"] == "no-store"
        assert (await admin.get(f"/custom/admin/{path}")).status_code == 200


@pytest.mark.parametrize(
    "module",
    ["state", "json", "controls", "main", "cards", "validation", "dialog"],
)
async def test_nested_config_sources_are_not_served(
    web_api: tuple[Quart, Database], module: str
) -> None:
    response = await web_api[0].test_client().get(f"/custom/admin/config/config-{module}.js")
    assert response.status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "fonts/DejaVuSans.ttf",
        "fonts/../common.css",
        "admin/not-a-module.js",
        "admin/config/not-a-module.js",
        "admin/../dashboard.js",
        "admin/charts.js",
        "admin/json-editor.js",
        "assets/admin/main.js.map",
        "frontend/src/legacy/admin/main.js",
        "frontend/package.json",
        "vendor/LICENSE_CODEMIRROR",
        "vendor/codemirror-5.65.16.min.js",
        "vendor/codemirror-5.65.16.min.css",
        "vendor/chart.umd.min.js",
        "assets/dashboard.js",
        "assets/history.js",
        "admin/chart.umd.min.js",
        "admin/vendor/codemirror-5.65.16.min.js",
        "admin/main.css",
        "admin/charts.css",
        "admin/vendor/codemirror-5.65.16.min.css",
        "admin.css",
        "charts.css",
    ],
)
async def test_unknown_assets(web_api: tuple[Quart, Database], path: str) -> None:
    assert (await web_api[0].test_client().get(f"/custom/{path}")).status_code == 404


@pytest.mark.parametrize(
    "path",
    ["/missing", "/custom/fonts/missing.woff2", "/custom/admin/missing.js"],
)
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
        assert '<script src="/custom/error.js?v=' in body
        assert "font-family: 'Outfit', sans-serif" in body
        assert "url('/custom/fonts/outfit-latin.woff2?v=" in body
        assert "/* __FONTS__ */" not in body
        code_style = body.split(".code {", 1)[1].split("}", 1)[0]
        assert "font-family" not in code_style
        assert "font-size: 13px" in code_style
        assert "color: var(--accent)" in code_style
        script = await web_api[0].test_client().get("/custom/error.js")
        assert script.status_code == 200
        assert script.mimetype == "text/javascript"
        assert '<link rel="stylesheet"' not in body
    else:
        assert response.mimetype == "application/json"
        assert (await response.get_json())["success"] is False


async def test_not_found_language_uses_query_then_cookie(web_api: tuple[Quart, Database]) -> None:
    client = web_api[0].test_client()
    headers = {"User-Agent": "Mozilla/5.0 Chrome/130.0 Safari/537.36"}
    client.set_cookie("localhost", "lang", "ru")
    russian = await client.get("/missing", headers=headers)
    body = await russian.get_data(as_text=True)
    assert russian.status_code == 404
    assert '<html lang="ru">' in body
    assert "Страница не найдена" in body
    english = await client.get("/missing?lang=en", headers=headers)
    body = await english.get_data(as_text=True)
    assert '<html lang="en">' in body
    assert "Page not found" in body


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
    assert '"language": "ru"' in await response.get_data(as_text=True)
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
