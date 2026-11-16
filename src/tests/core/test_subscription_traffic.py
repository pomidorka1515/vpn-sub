from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from core.services.panel import PanelService
from custom_types import BandwidthInfo
from errors import PanelUnavailableError
from tests.support.panels import FakePanel, make_panel_client
from tests.support.subscriptions import make_subscription
from tests.support.users import create_alice
from threadutil import named_to_thread

if TYPE_CHECKING:
    from core.services.panel import TrafficRow, TrafficSnapshot
    from db import Database
    from session.client import XUiSession
    from session.transport import PanelResponse

pytestmark = pytest.mark.asyncio


async def test_subscription_storm_shares_one_sample_across_users(database: Database) -> None:
    panel = FakePanel(
        clients=[make_panel_client(f"user{i}", [1], up=i, down=2) for i in range(320)]
    )
    sub = await make_subscription(database, panels=[panel])
    client = sub.panels[0]
    rows = await asyncio.gather(
        *(sub.panel_svc.subscription_traffic(client, f"user{i}") for i in range(320))
    )
    assert [row[4] for row in rows if row is not None] == list(range(320))
    assert panel.gets == ["panel/api/clients/list"]
    assert await sub.panel_svc.subscription_traffic(client, "missing") is None
    assert panel.gets == ["panel/api/clients/list"]


@pytest.mark.parametrize("reader", ["list_clients", "list_update_traffic"])
async def test_background_sample_is_reused_and_parse_runs_off_loop(
    database: Database, monkeypatch: pytest.MonkeyPatch, reader: str
) -> None:
    panel = FakePanel(clients=[make_panel_client("alice", [1], up=3, down=4)])
    sub = await make_subscription(database, panels=[panel])
    main_thread = threading.get_ident()
    parse = PanelService.response_data
    parsed = False

    def checked(panel: XUiSession, response: PanelResponse, what: str) -> dict[str, object]:
        nonlocal parsed
        assert threading.get_ident() != main_thread
        parsed = True
        return parse(panel, response, what)

    monkeypatch.setattr(PanelService, "response_data", staticmethod(checked))
    await getattr(sub.panel_svc, reader)(sub.panels[0])
    assert parsed
    row = await sub.panel_svc.subscription_traffic(sub.panels[0], "alice")
    assert row is not None
    assert row[4:] == (3, 4)
    assert panel.gets == ["panel/api/clients/list"]


async def test_subscription_sample_expires_and_invalidates(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel = FakePanel(clients=[make_panel_client("alice", [1], up=1, down=2)])
    sub = await make_subscription(database, panels=[panel])
    clock = 100.0
    monkeypatch.setattr("core.services.panel.monotonic", lambda: clock)
    client = sub.panels[0]
    await sub.panel_svc.subscription_traffic(client, "alice")
    panel.clients = [make_panel_client("alice", [1], up=5, down=6)]
    clock += 15
    row = await sub.panel_svc.subscription_traffic(client, "alice")
    assert row is not None
    assert row[4:] == (5, 6)
    panel.clients = []
    await sub.panel_svc.invalidate_clients(client)
    assert await sub.panel_svc.subscription_traffic(client, "alice") is None
    assert panel.gets == ["panel/api/clients/list"] * 3


async def test_traffic_consumers_share_refresh_and_expiry(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await create_alice(database)
    panel = FakePanel(clients=[make_panel_client("alice", [1], up=3, down=4)])
    sub = await make_subscription(database, panels=[panel])
    clock = 100.0
    monkeypatch.setattr("core.services.panel.monotonic", lambda: clock)
    client = sub.panels[0]

    rows, accounting, update = await asyncio.gather(
        sub.panel_svc.subscription_traffic(client, "alice"),
        sub.bandwidth_svc.panel_traffic(work="background"),
        sub.update_svc.snapshot(),
    )
    assert rows is not None
    assert rows[4:] == (3, 4)
    assert accounting[client.name]["alice"] == BandwidthInfo(3, 4, 7)
    assert update.totals["alice"] == [3, 4, 0, 0]
    assert panel.gets.count("panel/api/clients/list") == 1

    panel.clients = [make_panel_client("alice", [1], up=8, down=9)]
    clock += 14
    assert (await sub.bandwidth_svc.panel_traffic())[client.name]["alice"].upload == 3
    clock += 1
    assert (await sub.bandwidth_svc.panel_traffic())[client.name]["alice"].upload == 8
    assert panel.gets.count("panel/api/clients/list") == 2


async def test_subscription_bandwidth_filters_both_identities(database: Database) -> None:
    await create_alice(database)
    good = make_panel_client("alice", [1], up=5, down=6)
    assert good.traffic is not None
    panels = [
        FakePanel(name="good", clients=[good]),
        FakePanel(name="old-client", clients=[replace(good, uuid="old")]),
        FakePanel(
            name="old-traffic", clients=[replace(good, traffic=replace(good.traffic, uuid="old"))]
        ),
        FakePanel(
            name="wrong-email", clients=[replace(good, traffic=replace(good.traffic, email="bob"))]
        ),
    ]
    sub = await make_subscription(database, panels=panels)
    user = await database.get_user("alice")
    assert user is not None
    results = await asyncio.gather(
        *(sub.bandwidth_svc.subscription_bandwidth("alice", user["uuid"]) for _ in range(64))
    )
    assert results == [BandwidthInfo(5, 6, 11)] * 64
    assert all(panel.gets == ["panel/api/clients/list"] for panel in panels)


async def test_failed_sample_is_not_cached(database: Database) -> None:
    panel = FakePanel(get_error=PanelUnavailableError("offline"))
    sub = await make_subscription(database, panels=[panel])
    with pytest.raises(PanelUnavailableError):
        await sub.panel_svc.subscription_traffic(sub.panels[0], "alice")
    panel._get_error = None
    panel.clients = [make_panel_client("alice", [1], up=1, down=2)]
    assert await sub.panel_svc.subscription_traffic(sub.panels[0], "alice") is not None


async def test_invalidated_inflight_sample_cannot_repopulate_cache(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel = FakePanel(clients=[make_panel_client("alice", [1], up=1, down=2)])
    sub = await make_subscription(database, panels=[panel])
    entered = threading.Event()
    release = threading.Event()
    parse = PanelService._parse_traffic

    def blocked(
        panel: XUiSession, response: PanelResponse
    ) -> tuple[list[TrafficRow], TrafficSnapshot]:
        entered.set()
        assert release.wait(5)
        return parse(panel, response)

    monkeypatch.setattr(PanelService, "_parse_traffic", staticmethod(blocked))
    flight = asyncio.create_task(sub.panel_svc.subscription_traffic(sub.panels[0], "alice"))
    try:
        await named_to_thread("test:parse:wait", entered.wait)
        await sub.panel_svc.invalidate_clients(sub.panels[0])
        panel.clients = [make_panel_client("alice", [1], up=10, down=20)]
    finally:
        release.set()
    await flight
    assert sub.panels[0].name not in sub.panel_svc._traffic
    row = await sub.panel_svc.subscription_traffic(sub.panels[0], "alice")
    assert row is not None
    assert row[4:] == (10, 20)
