from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from core.services.panel import TRAFFIC_RETRY_DELAY, PanelService
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

    sub.update_svc.add_viewer("alice")
    rows, accounting, update = await asyncio.gather(
        sub.panel_svc.subscription_traffic(client, "alice"),
        sub.bandwidth_svc.panel_traffic(work="background"),
        sub.update_svc.snapshot("alice"),
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


async def test_blank_bulk_traffic_uuid_uses_enclosing_client(database: Database) -> None:
    await create_alice(database)
    client = make_panel_client("alice", [1], up=5, down=6)
    assert client.traffic is not None
    without_uuid = replace(client, traffic=replace(client.traffic, uuid=""))
    panel = FakePanel(clients=[without_uuid])
    sub = await make_subscription(database, panels=[panel])
    user = await database.get_user("alice")
    assert user is not None

    row = await sub.panel_svc.subscription_traffic(sub.panels[0], "alice")
    assert row == ("alice", user["uuid"], "alice", user["uuid"], 5, 6)
    assert await sub.bandwidth_svc.subscription_bandwidth("alice", user["uuid"]) == BandwidthInfo(
        5, 6, 11
    )


async def test_list_clients_republishes_blank_uuid_traffic(database: Database) -> None:
    await create_alice(database)
    client = make_panel_client("alice", [1], up=5, down=6)
    assert client.traffic is not None
    without_uuid = replace(client, traffic=replace(client.traffic, uuid=""))
    panel = FakePanel(clients=[without_uuid])
    sub = await make_subscription(database, panels=[panel])
    user = await database.get_user("alice")
    assert user is not None

    await sub.panel_svc.list_clients(sub.panels[0])
    row = await sub.panel_svc.subscription_traffic(sub.panels[0], "alice")
    assert row == ("alice", user["uuid"], "alice", user["uuid"], 5, 6)


async def test_failed_sample_retries_after_cooldown(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel = FakePanel(get_error=PanelUnavailableError("offline"))
    sub = await make_subscription(database, panels=[panel])
    clock = 100.0
    monkeypatch.setattr("core.services.panel.monotonic", lambda: clock)
    with pytest.raises(PanelUnavailableError):
        await sub.panel_svc.subscription_traffic(sub.panels[0], "alice")
    panel._get_error = None
    panel.clients = [make_panel_client("alice", [1], up=1, down=2)]
    with pytest.raises(PanelUnavailableError):
        await sub.panel_svc.subscription_traffic(sub.panels[0], "alice")
    clock += TRAFFIC_RETRY_DELAY
    assert await sub.panel_svc.subscription_traffic(sub.panels[0], "alice") is not None


@pytest.mark.parametrize(("age", "usable"), [(15, True), (74.999, True), (75, False)])
async def test_failed_refresh_uses_stale_sample_only_within_grace(
    database: Database, monkeypatch: pytest.MonkeyPatch, age: float, usable: bool
) -> None:
    panel = FakePanel(clients=[make_panel_client("alice", [1], up=1, down=2)])
    sub = await make_subscription(database, panels=[panel])
    clock = 100.0
    monkeypatch.setattr("core.services.panel.monotonic", lambda: clock)
    client = sub.panels[0]
    snapshot = await sub.panel_svc.traffic_snapshot(client)
    clock += age
    panel._get_error = PanelUnavailableError("offline")

    if usable:
        assert await sub.panel_svc.traffic_snapshot(client) is snapshot
        assert sub.panel_svc._traffic[client.name] == (100.0, snapshot)
        panel._get_error = None
        panel.clients = [make_panel_client("alice", [1], up=5, down=6)]
        assert await sub.panel_svc.traffic_snapshot(client) is snapshot
        clock += TRAFFIC_RETRY_DELAY
        refreshed = await sub.panel_svc.traffic_snapshot(client)
        assert refreshed.by_email["alice"][4:] == (5, 6)
    else:
        with pytest.raises(PanelUnavailableError, match="offline"):
            await sub.panel_svc.traffic_snapshot(client)


async def test_retry_cooldown_is_per_panel_and_respects_stale_expiry(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    panels = [
        FakePanel(name="one", clients=[make_panel_client("alice", [1])]),
        FakePanel(name="two", clients=[make_panel_client("alice", [1])]),
    ]
    sub = await make_subscription(database, panels=panels)
    clock = 100.0
    monkeypatch.setattr("core.services.panel.monotonic", lambda: clock)
    one, two = sub.panels
    snapshot = await sub.panel_svc.traffic_snapshot(one)
    await sub.panel_svc.traffic_snapshot(two)
    clock += 74
    panels[0]._get_error = PanelUnavailableError("offline")
    assert await sub.panel_svc.traffic_snapshot(one) is snapshot
    assert await sub.panel_svc.traffic_snapshot(one) is snapshot
    with pytest.raises(PanelUnavailableError, match="offline"):
        await sub.panel_svc.traffic_snapshot(one, allow_stale=False)
    assert len(panels[0].gets) == 2
    await sub.panel_svc.traffic_snapshot(two)
    assert len(panels[1].gets) == 2
    clock += 1
    with pytest.raises(PanelUnavailableError, match="offline"):
        await sub.panel_svc.traffic_snapshot(one)
    assert len(panels[0].gets) == 2
    clock += TRAFFIC_RETRY_DELAY - 1
    with pytest.raises(PanelUnavailableError, match="offline"):
        await sub.panel_svc.traffic_snapshot(one)
    assert len(panels[0].gets) == 3


async def test_invalidation_preserves_cooldown_without_serving_old_sample(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel = FakePanel(clients=[make_panel_client("alice", [1])])
    sub = await make_subscription(database, panels=[panel])
    clock = 100.0
    monkeypatch.setattr("core.services.panel.monotonic", lambda: clock)
    client = sub.panels[0]
    await sub.panel_svc.traffic_snapshot(client)
    clock += 15
    panel._get_error = PanelUnavailableError("offline")
    await sub.panel_svc.traffic_snapshot(client)
    await sub.panel_svc.invalidate_clients(client)
    with pytest.raises(PanelUnavailableError, match="offline"):
        await sub.panel_svc.traffic_snapshot(client)
    assert len(panel.gets) == 2
    clock += TRAFFIC_RETRY_DELAY
    panel._get_error = None
    await sub.panel_svc.traffic_snapshot(client)
    assert len(panel.gets) == 3


@pytest.mark.parametrize("change", ["none", "expire", "invalidate"])
async def test_failed_shared_refresh_checks_current_cache(
    database: Database, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    panel = FakePanel(clients=[make_panel_client("alice", [1], up=1, down=2)])
    sub = await make_subscription(database, panels=[panel])
    clock = 100.0
    monkeypatch.setattr("core.services.panel.monotonic", lambda: clock)
    client = sub.panels[0]
    snapshot = await sub.panel_svc.traffic_snapshot(client)
    clock += 15
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fail(_panel: XUiSession) -> tuple[list[TrafficRow], TrafficSnapshot]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        raise PanelUnavailableError("offline") from None

    monkeypatch.setattr(sub.panel_svc, "_load_traffic", fail)
    waiters = [asyncio.create_task(sub.panel_svc.traffic_snapshot(client)) for _ in range(20)]
    strict = asyncio.create_task(sub.panel_svc.traffic_snapshot(client, allow_stale=False))
    try:
        await entered.wait()
        if change == "expire":
            clock += 60
        elif change == "invalidate":
            await sub.panel_svc.invalidate_clients(client)
    finally:
        release.set()
    results = await asyncio.gather(*waiters, return_exceptions=True)
    strict_result = (await asyncio.gather(strict, return_exceptions=True))[0]
    assert calls == 1
    assert isinstance(strict_result, PanelUnavailableError)
    if change == "none":
        assert all(result is snapshot for result in results)
    else:
        assert all(isinstance(result, PanelUnavailableError) for result in results)


async def test_cancelling_waiter_does_not_return_stale_or_cancel_refresh(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel = FakePanel(clients=[make_panel_client("alice", [1], up=1, down=2)])
    sub = await make_subscription(database, panels=[panel])
    clock = 100.0
    monkeypatch.setattr("core.services.panel.monotonic", lambda: clock)
    client = sub.panels[0]
    snapshot = await sub.panel_svc.traffic_snapshot(client)
    clock += 15
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fail(_panel: XUiSession) -> tuple[list[TrafficRow], TrafficSnapshot]:
        entered.set()
        await release.wait()
        raise PanelUnavailableError("offline") from None

    monkeypatch.setattr(sub.panel_svc, "_load_traffic", fail)
    waiter = asyncio.create_task(sub.panel_svc.traffic_snapshot(client))
    try:
        await entered.wait()
        other = asyncio.create_task(sub.panel_svc.traffic_snapshot(client))
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    finally:
        release.set()
    assert await other is snapshot


async def test_failed_refresh_after_all_waiters_cancel_still_backs_off(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = await make_subscription(database, panels=[FakePanel()])
    clock = 100.0
    monkeypatch.setattr("core.services.panel.monotonic", lambda: clock)
    client = sub.panels[0]
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fail(_panel: XUiSession) -> tuple[list[TrafficRow], TrafficSnapshot]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        raise PanelUnavailableError("offline") from None

    monkeypatch.setattr(sub.panel_svc, "_load_traffic", fail)
    waiter = asyncio.create_task(sub.panel_svc.traffic_snapshot(client))
    try:
        await entered.wait()
        refresh = sub.panel_svc._traffic_tasks[client.name]
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    finally:
        release.set()
    with pytest.raises(PanelUnavailableError, match="offline"):
        await refresh
    with pytest.raises(PanelUnavailableError, match="offline"):
        await sub.panel_svc.traffic_snapshot(client)
    assert calls == 1


async def test_invalidated_old_failure_does_not_back_off_new_generation(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = await make_subscription(database, panels=[FakePanel()])
    clock = 100.0
    monkeypatch.setattr("core.services.panel.monotonic", lambda: clock)
    client = sub.panels[0]
    entered = asyncio.Event()
    release = asyncio.Event()
    load = sub.panel_svc._load_traffic
    calls = 0

    async def fail_old(panel: XUiSession) -> tuple[list[TrafficRow], TrafficSnapshot]:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            raise PanelUnavailableError("offline") from None
        return await load(panel)

    monkeypatch.setattr(sub.panel_svc, "_load_traffic", fail_old)
    old = asyncio.create_task(sub.panel_svc.traffic_snapshot(client, allow_stale=False))
    try:
        await entered.wait()
        await sub.panel_svc.invalidate_clients(client)
        await sub.panel_svc.traffic_snapshot(client)
    finally:
        release.set()
    with pytest.raises(PanelUnavailableError, match="offline"):
        await old
    assert client.name not in sub.panel_svc._traffic_retry_at
    clock += 15
    await sub.panel_svc.traffic_snapshot(client)
    assert calls == 3


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
