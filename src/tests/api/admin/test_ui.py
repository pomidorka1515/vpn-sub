from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest

from api import AsyncApi
from config.store import AsyncLinesConfig
from paths import frontend_root
from tests.support.api import Audit, api_app, config_snapshot, make_subscription, make_watch
from tests.support.config import subscription_config

if TYPE_CHECKING:
    from quart import Quart
    from quart.typing import TestClientProtocol

    from core import Subscription
    from db import Database

pytestmark = pytest.mark.asyncio

ADMIN_ASSETS = {
    "main.js": "assets/admin/main.js",
    "chart.umd.min.js": "vendor/chart.umd.min.js",
    "vendor/codemirror-5.65.16.min.js": "vendor/codemirror-5.65.16.min.js",
    "main.css": "assets/admin.css",
    "charts.css": "assets/charts.css",
    "vendor/codemirror-5.65.16.min.css": "vendor/codemirror-5.65.16.min.css",
}


async def sign_in(client: TestClientProtocol) -> None:
    response = await client.post(
        "/sub/admin/session",
        json={"username": "admin", "password": "panel-secret"},
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    for part in ("admin_ui=", "Path=/sub/admin", "HttpOnly", "Secure", "SameSite=Lax"):
        assert part in response.headers["Set-Cookie"]


async def browser_token(client: TestClientProtocol) -> str:
    response = await client.get("/sub/admin/token")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    token = (await response.get_json())["obj"]["token"]
    assert isinstance(token, str)
    assert token.startswith("ui.")
    assert token != "secret"
    return token


async def test_admin_ui_requires_session(admin_api: tuple[Quart, Subscription]) -> None:
    client = admin_api[0].test_client()
    response = await client.get("/sub/admin")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/sub/admin/login")
    response = await client.get("/sub/admin/token")
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    response = await client.get("/sub/admin/login")
    body = await response.get_data()
    assert isinstance(body, bytes)
    for part in (
        b'id="loginForm"',
        b'<script type="module" src="/sub/admin-auth.js?v=',
    ):
        assert part in body
    for part in (b'id="tabRegister"', b'id="registerForm"', b"const API", b"PANEL + '/'"):
        assert part not in body


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("cookie", [None, "invalid", "x" * 100])
@pytest.mark.parametrize("path", ADMIN_ASSETS)
async def test_admin_assets_require_session(
    admin_api: tuple[Quart, Subscription], method: str, cookie: str | None, path: str
) -> None:
    client = admin_api[0].test_client()
    if cookie is not None:
        client.set_cookie("localhost", "admin_ui", cookie, path="/sub/admin")
    request_asset = client.get if method == "GET" else client.head
    response = await request_asset(
        f"/sub/admin/{path}?v=test",
        headers={"Authorization": "secret", "If-None-Match": "*"},
    )
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    body = await response.get_data()
    assert isinstance(body, bytes)
    assert b"import.meta.url" not in body


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize(("path", "resource"), ADMIN_ASSETS.items())
async def test_admin_assets_with_session_are_not_stored(
    admin_api: tuple[Quart, Subscription], method: str, path: str, resource: str
) -> None:
    client = admin_api[0].test_client()
    await sign_in(client)
    request_asset = client.get if method == "GET" else client.head
    response = await request_asset(f"/sub/admin/{path}?v=test", headers={"If-None-Match": "*"})
    assert response.status_code == 200
    assert response.mimetype == ("text/css" if path.endswith(".css") else "text/javascript")
    assert response.headers["Cache-Control"] == "no-store"
    assert "ETag" not in response.headers
    body = await response.get_data()
    assert isinstance(body, bytes)
    if method == "GET":
        assert body == (frontend_root() / resource).read_bytes()
        if path == "main.js":
            assert b"import.meta.url" in body
            assert b"sourceMappingURL" not in body
            assert b"// src/" not in body
            assert b"\n" not in body


@pytest.mark.parametrize(
    "body",
    [
        {"username": "admin", "password": "nope"},
        {"username": "admin", "password": "x"},
        {"username": "admin"},
        {"username": "", "password": ""},
    ],
)
async def test_admin_session_rejects_invalid_login(
    admin_api: tuple[Quart, Subscription],
    body: dict[str, str],
) -> None:
    response = await admin_api[0].test_client().post("/sub/admin/session", json=body)
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"


async def test_admin_session_and_local_page(admin_api: tuple[Quart, Subscription]) -> None:
    app, sub = admin_api
    client = app.test_client()
    response = await client.post(
        "/sub/admin/session",
        data="username=admin",
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 400
    assert response.headers["Cache-Control"] == "no-store"
    await sign_in(client)
    session = await sub.res.db.admin_ui_session()
    assert session is not None
    assert len(session) == 100
    response = await client.get("/sub/admin")
    assert response.status_code == 200
    assert (await client.get("/sub/admin/")).status_code == 200
    body = await response.get_data()
    assert isinstance(body, bytes)
    for part in (
        b"<html",
        b'href="/sub/common.css?v=',
        b'href="/sub/admin/main.css?v=',
        b'href="/sub/admin/charts.css?v=',
        b'href="/sub/admin/vendor/codemirror-5.65.16.min.css?v=',
        b'type="module"',
        b'src="/sub/admin/main.js?v=',
        b'id="btnLogout"',
        b'"chartUrl": "/sub/admin/chart.umd.min.js?v=',
        b'id="frontend-bootstrap" type="application/json"',
        b'"prefix": "/sub"',
    ):
        assert part in body
    for part in (b"fonts.googleapis", b"fonts.gstatic", b"jsdelivr"):
        assert part not in body
    response = await client.get("/sub/admin/token")
    payload = (await response.get_json())["obj"]
    assert payload.pop("permissions") == ["*"]
    assert payload.pop("catalog")
    assert payload == {
        "token": await browser_token(client),
        "api_root": "/sub/api",
        "panels_configured": True,
    }


async def test_expired_admin_cookie_cannot_be_replayed(
    admin_api: tuple[Quart, Subscription],
) -> None:
    app, sub = admin_api
    client = app.test_client()
    await sign_in(client)
    session = await sub.res.db.admin_ui_session()
    assert session is not None
    await sub.res.db.set_metadata("admin_ui_session_expires_at", "1")
    client.set_cookie("localhost", "admin_ui", session, path="/sub/admin")
    assert (await client.get("/sub/admin")).status_code == 302
    assert (await client.get("/sub/admin/token")).status_code == 401


async def test_admin_relogin_and_logout(admin_api: tuple[Quart, Subscription]) -> None:
    app, sub = admin_api
    client = app.test_client()
    await sign_in(client)
    first = await sub.res.db.admin_ui_session()
    await sign_in(client)
    assert first != await sub.res.db.admin_ui_session()
    assert first is not None
    client.set_cookie("localhost", "admin_ui", first, path="/sub/admin")
    assert (await client.get("/sub/admin")).status_code == 302
    await sign_in(client)
    response = await client.post(
        "/sub/admin/logout",
        headers={"X-Admin-CSRF": await browser_token(client)},
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "Path=/sub/admin" in response.headers["Set-Cookie"]
    assert "Max-Age=0" in response.headers["Set-Cookie"]
    assert await sub.res.db.admin_ui_session() is None
    assert (await client.get("/sub/admin")).status_code == 302
    assert (await client.get("/sub/admin/token")).status_code == 401


@pytest.mark.parametrize("revoke", ["logout", "relogin", "expiry"])
async def test_browser_api_authority_is_session_bound(
    admin_api: tuple[Quart, Subscription],
    revoke: str,
) -> None:
    app, sub = admin_api
    client = app.test_client()
    await sign_in(client)
    cookie = await sub.res.db.admin_ui_session()
    assert cookie is not None
    token = await browser_token(client)
    headers = {"Authorization": token}
    # Bearer auth needs an explicit non-simple header, never just ambient cookies.
    assert (await client.get("/sub/api/api/config/get")).status_code == 401
    response = await client.get("/sub/api/api/config/get", headers=headers)
    assert response.status_code == 200
    assert "api_token" not in (await response.get_json())["obj"]
    assert (
        await app.test_client().get("/sub/api/api/config/get", headers=headers)
    ).status_code == 200
    if revoke == "logout":
        for csrf in ({}, {"X-Admin-CSRF": "wrong"}):
            response = await client.post("/sub/admin/logout", headers=csrf)
            assert response.status_code == 403
            assert response.headers["Cache-Control"] == "no-store"
        response = await client.post("/sub/admin/logout", headers={"X-Admin-CSRF": token})
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
    elif revoke == "relogin":
        await sign_in(client)
    else:
        await sub.res.db.set_metadata("admin_ui_session_expires_at", "1")
    client.set_cookie("localhost", "admin_ui", cookie, path="/sub/admin")
    response = await client.get("/sub/admin/token")
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    assert (await client.get("/sub/api/api/config/get", headers=headers)).status_code == 401
    for path in ADMIN_ASSETS:
        response = await client.get(f"/sub/admin/{path}?v=test", headers={"If-None-Match": "*"})
        assert response.status_code == 401
        assert response.headers["Cache-Control"] == "no-store"
    response = await client.get("/sub/api/api/config/get", headers={"Authorization": "secret"})
    assert response.status_code == 200
    assert (await response.get_json())["obj"]["api_token"] == "secret"


@pytest.mark.parametrize("cookie", [None, "invalid", "x" * 100, "stale"])
async def test_admin_logout_rejects_unauthenticated_callers(
    admin_api: tuple[Quart, Subscription],
    cookie: str | None,
) -> None:
    app, sub = admin_api
    admin = app.test_client()
    await sign_in(admin)
    if cookie == "stale":
        cookie = await sub.res.db.admin_ui_session()
        await sign_in(admin)
    session = await sub.res.db.admin_ui_session()
    attacker = app.test_client()
    if cookie is not None:
        attacker.set_cookie("localhost", "admin_ui", cookie, path="/sub/admin")

    response = await attacker.post("/sub/admin/logout")

    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    assert "Set-Cookie" not in response.headers
    assert await sub.res.db.admin_ui_session() == session
    assert (await admin.get("/sub/admin/token")).status_code == 200


async def test_admin_logout_is_rate_limited(
    admin_api: tuple[Quart, Subscription],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, sub = admin_api
    admin = app.test_client()
    await sign_in(admin)
    session = await sub.res.db.admin_ui_session()
    allow = AsyncMock(side_effect=[0] * 10 + [60])
    monkeypatch.setattr(app.extensions["limiter"], "retry_after", allow)
    attacker = app.test_client()
    for _ in range(10):
        assert (await attacker.post("/sub/admin/logout")).status_code == 401
    response = await attacker.post("/sub/admin/logout")
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert response.headers["Cache-Control"] == "no-store"
    assert allow.await_count == 11
    for call in allow.await_args_list:
        assert call.args[0].endswith("AsyncUiRoutes.admin_logout:10")
        assert call.args[2] == 10
    assert await sub.res.db.admin_ui_session() == session
    assert (await admin.get("/sub/admin/token")).status_code == 200


@pytest.mark.parametrize("endpoint", ["session", "logout"])
@pytest.mark.parametrize("store_failed", [False, True])
async def test_admin_auth_rate_limit_responses_are_not_stored(
    admin_api: tuple[Quart, Subscription],
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    store_failed: bool,
) -> None:
    app, _ = admin_api
    allow = AsyncMock(return_value=60, side_effect=RuntimeError() if store_failed else None)
    monkeypatch.setattr(app.extensions["limiter"], "retry_after", allow)

    response = await app.test_client().post(f"/sub/admin/{endpoint}")

    assert response.status_code == 429
    assert response.headers.get("Retry-After") == (None if store_failed else "60")
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("endpoint", ["token", "session", "logout", *ADMIN_ASSETS])
async def test_admin_auth_storage_errors_are_not_stored(
    admin_api: tuple[Quart, Subscription],
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    app, sub = admin_api
    client = app.test_client()
    await sign_in(client)
    token = await browser_token(client)
    operation = "set_admin_ui_session" if endpoint in {"session", "logout"} else "admin_ui_session"
    monkeypatch.setattr(sub.res.db, operation, AsyncMock(side_effect=RuntimeError()))

    if endpoint not in {"session", "logout"}:
        response = await client.get(f"/sub/admin/{endpoint}")
    else:
        response = await client.post(
            f"/sub/admin/{endpoint}",
            json={"username": "admin", "password": "panel-secret"},
            headers={"X-Admin-CSRF": token},
        )

    assert response.status_code == 500
    assert response.headers["Cache-Control"] == "no-store"
    assert "Set-Cookie" not in response.headers


@pytest.mark.parametrize("authenticated", [False, True])
async def test_admin_token_head_is_not_stored(
    admin_api: tuple[Quart, Subscription],
    authenticated: bool,
) -> None:
    client = admin_api[0].test_client()
    if authenticated:
        await sign_in(client)

    response = await client.head("/sub/admin/token")

    assert response.status_code == (200 if authenticated else 401)
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(("uri", "api_uri", "root"), [("", "api", "/api"), ("sub", "", "/sub")])
async def test_admin_empty_uri_contract(
    database: Database,
    uri: str,
    api_uri: str,
    root: str,
) -> None:
    async with api_app() as app:
        sub = await make_subscription(database, app=app, uri=uri)
        AsyncApi(
            app=app,
            cfg=config_snapshot(
                subscription_config(
                    uri=uri,
                    api_uri=api_uri,
                    api_token="secret",
                    api_admin_ui_auth=["admin", "panel-secret"],
                ),
            ),
            audit_cfg=cast(AsyncLinesConfig, Audit()),
            sub=sub,
            bw=make_watch(database, sub),
            limiter=app.extensions["limiter"],
        )
        client = app.test_client()
        panel = f"/{uri}/admin" if uri else "/admin"
        response = await client.post(
            f"{panel}/session",
            json={"username": "admin", "password": "panel-secret"},
        )
        assert response.status_code == 200
        assert f"Path={panel}" in response.headers["Set-Cookie"]
        for path in ADMIN_ASSETS:
            response = await client.get(f"{panel}/{path}")
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store"
        response = await client.get(f"{panel}/token")
        assert response.status_code == 200
        payload = await response.get_json()
        assert payload["success"] is True
        token = payload["obj"]["token"]
        assert token.startswith("ui.")
        assert payload["obj"].pop("permissions") == ["*"]
        assert payload["obj"].pop("catalog")
        assert payload["obj"] == {"token": token, "api_root": root, "panels_configured": True}
        response = await client.post(f"{panel}/logout", headers={"X-Admin-CSRF": token})
        assert response.status_code == 200
        assert f"Path={panel}" in response.headers["Set-Cookie"]
        assert "Max-Age=0" in response.headers["Set-Cookie"]
