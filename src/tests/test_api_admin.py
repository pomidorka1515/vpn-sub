from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import pytest_asyncio
from async_api_helpers import api_app, config_snapshot, make_subscription, make_watch
from helpers import FakePanel, subscription_config

from api import AsyncApi
from config import Config, LangConfig
from config.store import AsyncLinesConfig
from errors import AppError, DatabaseError, PanelUnavailableError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from quart import Quart
    from quart.testing.connections import TestHTTPConnection
    from quart.typing import TestClientProtocol

    from core import Subscription
    from db import Database

pytestmark = pytest.mark.asyncio
LANG_PATH = Path(__file__).resolve().parents[2] / "lang.jsonc"


class Audit:
    async def read_all(self) -> list[dict[str, str]]:
        return []

    async def tail(self, n: int) -> list[dict[str, str]]:
        return []


@pytest_asyncio.fixture
async def admin_api(
    database: Database,
) -> AsyncIterator[tuple[Quart, Subscription]]:
    async for app in api_app():
        language = Config[LangConfig](path=LANG_PATH, read_only=True, read_only_jsonc=True)
        sub = await make_subscription(database, app=app, lang_cfg=language)
        AsyncApi(
            app=app,
            cfg=config_snapshot(
                subscription_config(
                    api_uri="api",
                    api_token="secret",
                    api_admin_ui_auth=["admin", "panel-secret"],
                ),
            ),
            audit_cfg=cast(AsyncLinesConfig, Audit()),
            sub=sub,
            bw=make_watch(database, sub),
            limiter=app.extensions["limiter"],
        )
        yield app, sub
        language.close()


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
    assert (await response.get_json())["obj"] == {"token": "secret", "api_root": "/sub/api"}


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
    assert "Path=/sub/admin" in response.headers["Set-Cookie"]
    assert "Max-Age=0" in response.headers["Set-Cookie"]
    assert await sub.res.db.admin_ui_session() is None
    assert (await client.get("/sub/admin")).status_code == 302
    assert (await client.get("/sub/admin/token")).status_code == 401


async def test_user_refresh_aborts_on_non_panel_error(
    admin_api: tuple[Quart, Subscription],
) -> None:
    app, sub = admin_api
    for name in ("alice", "bob"):
        await sub.res.db.create_user(
            username=name,
            uuid=name,
            token=name,
            fingerprint="chrome",
            displayname=name,
        )

    async def add_users(
        username: str,
        _called_internally: bool = False,
        *,
        work: str = "foreground",
    ) -> None:
        if username == "bob":
            raise AppError("panel rejected")

    sub.business_svc.add_users = add_users  # type: ignore[method-assign]
    response = await app.test_client().get(
        "/sub/api/api/user/refresh",
        headers={"Authorization": "secret"},
    )
    assert response.status_code == 500
    payload = await response.get_json()
    assert payload["msg"] == "Refresh aborted"
    assert payload["obj"] == {"aborted": "bob", "failed": [], "succeeded": 1, "total": 2}


@pytest.mark.parametrize("user_count", [0, 1, 9])
async def test_user_refresh_uses_bounded_workers(
    admin_api: tuple[Quart, Subscription],
    monkeypatch: pytest.MonkeyPatch,
    user_count: int,
) -> None:
    app, sub = admin_api
    users = [f"user{index}" for index in range(user_count)]
    started: list[str] = []
    active = 0
    peak = 0
    saturated = asyncio.Event()
    release = asyncio.Event()

    async def list_users() -> list[str]:
        return users

    async def add_users(username: str) -> None:
        nonlocal active, peak
        started.append(username)
        active += 1
        peak = max(peak, active)
        if active == min(4, user_count):
            saturated.set()
        try:
            await release.wait()
        finally:
            active -= 1

    monkeypatch.setattr(sub.user_svc, "list_users", list_users)
    monkeypatch.setattr(sub.business_svc, "add_users", add_users)
    refresh = asyncio.create_task(
        app.test_client().get(
            "/sub/api/api/user/refresh",
            headers={"Authorization": "secret"},
        )
    )
    try:
        if users:
            async with asyncio.timeout(5):
                await saturated.wait()
            assert started == users[:4]
            workers = [
                task
                for task in asyncio.all_tasks()
                if task.get_name().startswith("core:user-refresh-worker:")
            ]
            assert len(workers) == min(4, user_count)
        release.set()
        async with asyncio.timeout(5):
            response = await refresh
        assert response.status_code == 200
        assert (await response.get_json())["msg"] == "Refreshed all users."
        assert sorted(started) == users
        assert peak == min(4, user_count)
    finally:
        release.set()
        await refresh


@pytest.mark.parametrize("all_failed", [False, True])
async def test_user_refresh_reports_panel_failures_in_user_order(
    admin_api: tuple[Quart, Subscription],
    monkeypatch: pytest.MonkeyPatch,
    all_failed: bool,
) -> None:
    app, sub = admin_api
    users = [f"user{index}" for index in range(9)]
    first_failed = asyncio.Event()

    async def list_users() -> list[str]:
        return users

    async def add_users(username: str) -> None:
        if username == "user0":
            await first_failed.wait()
        if all_failed or username in {"user0", "user1"}:
            first_failed.set()
            raise PanelUnavailableError("offline")

    monkeypatch.setattr(sub.user_svc, "list_users", list_users)
    monkeypatch.setattr(sub.business_svc, "add_users", add_users)
    async with asyncio.timeout(5):
        response = await app.test_client().get(
            "/sub/api/api/user/refresh",
            headers={"Authorization": "secret"},
        )
    assert response.status_code == (502 if all_failed else 200)
    payload = await response.get_json()
    if all_failed:
        assert payload["obj"] == {"failed": users, "total": 9}
    else:
        assert payload["obj"] == {"failed": users[:2], "succeeded": 7, "total": 9}


async def test_user_refresh_abort_drains_in_flight_and_stops_new_users(
    admin_api: tuple[Quart, Subscription],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, sub = admin_api
    users = ["alice", "bob", "carol", "dave", "eve", "frank"]
    started: list[str] = []
    finished: list[str] = []
    saturated = asyncio.Event()
    aborted = asyncio.Event()
    release = asyncio.Event()

    async def list_users() -> list[str]:
        return users

    async def add_users(username: str) -> None:
        started.append(username)
        if len(started) == 4:
            saturated.set()
        try:
            if username == "bob":
                await saturated.wait()
                aborted.set()
                raise AppError("unexpected failure")
            await release.wait()
            if username == "alice":
                raise PanelUnavailableError("offline")
            if username == "carol":
                raise AppError("another in-flight failure")
        finally:
            finished.append(username)

    monkeypatch.setattr(sub.user_svc, "list_users", list_users)
    monkeypatch.setattr(sub.business_svc, "add_users", add_users)
    refresh = asyncio.create_task(
        app.test_client().get(
            "/sub/api/api/user/refresh",
            headers={"Authorization": "secret"},
        )
    )
    try:
        async with asyncio.timeout(5):
            await aborted.wait()
        assert not refresh.done()
        assert finished == ["bob"]
        release.set()
        async with asyncio.timeout(5):
            response = await refresh
        assert response.status_code == 500
        payload = await response.get_json()
        assert payload["msg"] == "Refresh aborted"
        assert payload["obj"] == {
            "aborted": "bob",
            "failed": ["alice"],
            "succeeded": 1,
            "total": 6,
        }
        assert started == users[:4]
        assert sorted(finished) == users[:4]
    finally:
        release.set()
        await refresh


async def test_health_reports_db_and_process_status(
    admin_api: tuple[Quart, Subscription],
) -> None:
    client = admin_api[0].test_client()
    assert (await client.get("/sub/api/api/health")).status_code == 401
    response = await client.get("/sub/api/api/health", headers={"Authorization": "secret"})
    assert response.status_code == 200
    payload = await response.get_json()
    assert payload["success"] is True
    obj = payload["obj"]
    assert obj["db"] is True
    assert obj["degraded"] is False
    assert isinstance(obj["uptime"], float)
    assert isinstance(obj["threads"], int)
    assert set(obj["memory"]) == {"ram", "swap"}
    assert obj["daily_snapshot_failure"] is None
    assert obj["rollback_failures"] == {"uuid": {}, "registration": {}}


async def test_health_returns_503_when_database_ping_fails(
    admin_api: tuple[Quart, Subscription],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> object:
        raise DatabaseError("down")

    app, sub = admin_api
    monkeypatch.setattr(sub.res.db, "connection", fail)
    response = await app.test_client().get(
        "/sub/api/api/health",
        headers={"Authorization": "secret"},
    )
    assert response.status_code == 503
    payload = await response.get_json()
    assert payload["success"] is False
    assert payload["msg"] == "Database unavailable"
    assert payload["obj"]["db"] is False


@pytest.mark.parametrize(("uri", "api_uri", "root"), [("", "api", "/api"), ("sub", "", "/sub")])
async def test_admin_empty_uri_contract(
    database: Database,
    uri: str,
    api_uri: str,
    root: str,
) -> None:
    async for app in api_app():
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
        assert payload["obj"] == {"token": "secret", "api_root": root}
        response = await client.post(f"{panel}/logout")
        assert response.status_code == 200
        assert f"Path={panel}" in response.headers["Set-Cookie"]
        assert "Max-Age=0" in response.headers["Set-Cookie"]


@pytest.mark.parametrize("polling", [True, False])
async def test_state_collects_panels_and_host_concurrently(
    admin_api: tuple[Quart, Subscription],
    monkeypatch: pytest.MonkeyPatch,
    polling: bool,
) -> None:
    from api.admin.state import AsyncStateRoutes

    @dataclass
    class Host:
        cpu: float = 1.5

    app, sub = admin_api
    panels_started = asyncio.Event()
    host_started = asyncio.Event()

    async def statuses() -> list[None]:
        panels_started.set()
        await host_started.wait()
        return []

    async def metrics_run(*args: object, **kwargs: object) -> Host:
        host_started.set()
        await panels_started.wait()
        return Host()

    monkeypatch.setattr(sub.panel_svc, "statuses", statuses)
    monkeypatch.setattr(sub.res.offloads.metrics, "run", metrics_run)
    async with asyncio.timeout(5):
        if polling:
            routes = AsyncStateRoutes()
            routes.sub = sub
            payload = await routes._polling_sample()
        else:
            response = await app.test_client().get(
                "/sub/api/api/state/all",
                headers={"Authorization": "secret"},
            )
            assert response.status_code == 200
            payload = (await response.get_json())["obj"]
    assert payload == {"host": {"cpu": 1.5}, "panels": {}}


async def test_polling_status_contract(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    async for app in api_app():
        sub = await make_subscription(
            database,
            app=app,
            panels=[panel, FakePanel(name="down", status_error=RuntimeError("down"))],
        )
        AsyncApi(
            app=app,
            cfg=config_snapshot(subscription_config(api_uri="api", api_token="secret")),
            audit_cfg=cast(AsyncLinesConfig, Audit()),
            sub=sub,
            bw=make_watch(database, sub),
            limiter=app.extensions["limiter"],
        )
        client = app.test_client()
        assert (await client.get("/sub/api/api/state/polling")).status_code == 401
        async with client.request(  # pyright: ignore[reportUnknownMemberType]  # Quart's request signature includes unparameterized dicts.
            "/sub/api/api/state/polling",
            method="GET",
            headers={"Authorization": "secret"},
        ) as connection:
            connection = cast("TestHTTPConnection", connection)
            async with asyncio.timeout(5):
                body = b""
                while b"data: " not in body or not body.endswith(b"\n\n"):
                    body += await connection.receive()
            assert connection.status_code == 200
            assert connection.headers is not None
            assert connection.headers["Content-Type"] == "text/event-stream"
            assert connection.headers["X-Accel-Buffering"] == "no"
            payload = json.loads(body.split(b"data: ", 1)[1])
            await connection.disconnect()
        assert payload["success"] is True
        obj = payload["obj"]
        assert set(obj) == {"host", "panels"}
        assert obj["host"] == {
            "cpu": 1.5,
            "process_count": 4,
            "uptime": 1000.5,
            "loadavg": {"load_1m": 0.1, "load_5m": 0.2, "load_15m": 0.3},
            "network": {"sent": 1, "recv": 2},
            "memory": {
                "ram": {"total": 3, "available": 2, "used": 1},
                "swap": {"total": 0, "free": 0, "used": 0},
            },
            "connections": {"tcp": 5, "udp": 6},
            "app_memory": {"ram": 7, "swap": 0},
            "app_uptime": 200.25,
            "app_thread_amount": 1,
            "app_threads": [],
        }
        assert obj["panels"] == {
            "edge": {
                "app_stats": {"threads": 2, "mem": 3, "uptime": 4},
                "cpu": 12.5,
                "disk": {"current": 3, "total": 4},
                "loads": [0.1, 0.2, 0.3],
                "mem": {"current": 10, "total": 20},
                "netIO": {"up": 1, "down": 2},
                "netTraffic": {"sent": 9, "recv": 10},
                "swap": {"current": 1, "total": 2},
                "tcpCount": 7,
                "udpCount": 8,
                "uptime": 99,
            },
            "down": None,
        }
