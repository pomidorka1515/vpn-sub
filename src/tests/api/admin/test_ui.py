from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest

from api import AsyncApi
from config.store import AsyncLinesConfig
from tests.support.api import Audit, api_app, config_snapshot, make_subscription, make_watch
from tests.support.config import subscription_config

if TYPE_CHECKING:
    from quart import Quart
    from quart.typing import TestClientProtocol

    from core import Subscription
    from db import Database

pytestmark = pytest.mark.asyncio


async def sign_in(client: TestClientProtocol) -> None:
    response = await client.post(
        "/sub/admin/session",
        json={"username": "admin", "password": "panel-secret"},
    )
    assert response.status_code == 200
    for part in ("admin_ui=", "Path=/sub/admin", "HttpOnly", "Secure", "SameSite=Lax"):
        assert part in response.headers["Set-Cookie"]


async def test_admin_ui_requires_session(admin_api: tuple[Quart, Subscription]) -> None:
    client = admin_api[0].test_client()
    response = await client.get("/sub/admin")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/sub/admin/login")
    assert (await client.get("/sub/admin/token")).status_code == 401
    response = await client.get("/sub/admin/login")
    body = await response.get_data()
    assert isinstance(body, bytes)
    for part in (
        b'id="loginForm"',
        b"const SESSION = PANEL + '/session'",
        b"PANEL + '/token'",
        b"window.location.href = PANEL;",
    ):
        assert part in body
    for part in (b'id="tabRegister"', b'id="registerForm"', b"const API", b"PANEL + '/'"):
        assert part not in body


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
    assert (
        await admin_api[0].test_client().post("/sub/admin/session", json=body)
    ).status_code == 401


async def test_admin_session_and_local_page(admin_api: tuple[Quart, Subscription]) -> None:
    app, sub = admin_api
    client = app.test_client()
    assert (
        await client.post(
            "/sub/admin/session",
            data="username=admin",
            headers={"Content-Type": "text/plain"},
        )
    ).status_code == 400
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
        b'href="/sub/admin.css?v=',
        b'type="module"',
        b'src="/sub/admin/main.js?v=',
        b'id="btnLogout"',
        b"window.CHART_JS = '/sub/chart.umd.min.js?v=",
    ):
        assert part in body
    for part in (b"fonts.googleapis", b"fonts.gstatic", b"jsdelivr"):
        assert part not in body
    response = await client.get("/sub/admin/token")
    assert (await response.get_json())["obj"] == {
        "token": "secret",
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
    response = await client.post("/sub/admin/logout")
    assert response.status_code == 200
    assert "Path=/sub/admin" in response.headers["Set-Cookie"]
    assert "Max-Age=0" in response.headers["Set-Cookie"]
    assert await sub.res.db.admin_ui_session() is None
    assert (await client.get("/sub/admin")).status_code == 302
    assert (await client.get("/sub/admin/token")).status_code == 401


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
    allow = AsyncMock(side_effect=[True] * 10 + [False])
    monkeypatch.setattr(app.extensions["limiter"], "allow", allow)
    attacker = app.test_client()
    for _ in range(10):
        assert (await attacker.post("/sub/admin/logout")).status_code == 401
    assert (await attacker.post("/sub/admin/logout")).status_code == 429
    assert allow.await_count == 11
    for call in allow.await_args_list:
        assert call.args[0].endswith("AsyncUiRoutes.admin_logout:10")
        assert call.args[2] == 10
    assert await sub.res.db.admin_ui_session() == session
    assert (await admin.get("/sub/admin/token")).status_code == 200


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
        response = await client.get(f"{panel}/token")
        assert response.status_code == 200
        payload = await response.get_json()
        assert payload["success"] is True
        assert payload["obj"] == {"token": "secret", "api_root": root, "panels_configured": True}
        response = await client.post(f"{panel}/logout")
        assert response.status_code == 200
        assert f"Path={panel}" in response.headers["Set-Cookie"]
        assert "Max-Age=0" in response.headers["Set-Cookie"]
