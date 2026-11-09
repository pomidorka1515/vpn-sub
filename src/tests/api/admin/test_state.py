from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest
from quart.testing.connections import TestHTTPConnection as HTTPTestConnection

from api import AsyncApi
from config.store import AsyncLinesConfig
from errors import DatabaseError
from tests.support.api import Audit, api_app, config_snapshot, make_subscription, make_watch
from tests.support.config import subscription_config
from tests.support.panels import FakePanel

if TYPE_CHECKING:
    from quart import Quart

    from core import Subscription
    from db import Database

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "cutoff",
    [2**70, 2**63 - 1, (2**63 - 1) // 86400 + 1, float("inf"), float("nan")],
)
async def test_snapshot_cutoff_bounds(
    admin_api: tuple[Quart, Subscription],
    cutoff: float,
) -> None:
    response = (
        await admin_api[0]
        .test_client()
        .post(
            "/sub/api/api/state/snapshots",
            headers={"Authorization": "secret"},
            json={"cutoff": cutoff},
        )
    )
    assert response.status_code == 400
    assert "cutoff" in (await response.get_json())["msg"]


async def test_snapshot_largest_safe_cutoff(admin_api: tuple[Quart, Subscription]) -> None:
    response = (
        await admin_api[0]
        .test_client()
        .post(
            "/sub/api/api/state/snapshots",
            headers={"Authorization": "secret"},
            json={"cutoff": (2**63 - 1) // 86400},
        )
    )
    assert response.status_code == 200


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
    assert obj["pending_recovery"] == {
        "registration": [],
        "create": [],
        "delete": [],
        "reset": [],
        "sync": [],
    }


@pytest.mark.parametrize("kind", ["create", "delete", "reset", "sync"])
async def test_health_and_operations_report_pending_recovery(
    admin_api: tuple[Quart, Subscription],
    kind: str,
) -> None:
    app, sub = admin_api
    key = f"user_{kind}_pending:alice"
    # Even malformed intent values must be reported, never parsed or exposed.
    await sub.res.db.set_metadata(key, "private recovery payload")
    client = app.test_client()
    headers = {"Authorization": "secret"}
    health = await (await client.get("/sub/api/api/health", headers=headers)).get_json()
    operations = await (
        await client.get("/sub/api/api/operations/status", headers=headers)
    ).get_json()
    assert health["obj"]["degraded"] is True
    assert health["obj"]["rollback_failures"] == {"uuid": {}, "registration": {}}
    pending = health["obj"]["pending_recovery"]
    assert pending[kind] == ["alice"]
    assert operations["obj"]["pending_recovery"] == pending
    assert "private recovery payload" not in str(health) + str(operations)
    rejected = await client.post(
        "/sub/api/api/operations/rollback/resolve",
        headers=headers,
        json={"kind": kind, "user": "alice"},
    )
    assert (await rejected.get_json())["success"] is False
    assert await sub.res.db.get_metadata(key) == "private recovery payload"
    await sub.res.db.delete_metadata(key)
    recovered = await (await client.get("/sub/api/api/health", headers=headers)).get_json()
    assert recovered["obj"]["degraded"] is False


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
    for panel in sub.panels:
        await panel.close()
    sub.panels.clear()

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
    async with api_app() as app:
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
            connection = cast(HTTPTestConnection, connection)
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
