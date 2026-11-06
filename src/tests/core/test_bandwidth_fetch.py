from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from custom_types import BandwidthInfo
from errors import PanelUnavailableError
from tests.support.panels import FakePanel, make_client, make_panel_client
from tests.support.subscriptions import make_subscription
from tests.support.users import USER_UUID, create_alice

if TYPE_CHECKING:
    from custom_types import ClientTraffic
    from db import Database
    from session.client import XUiSession


NEW_UUID = "ffffffff-ffff-4fff-8fff-ffffffffffff"


@pytest.mark.asyncio
async def test_whitelist_total_uses_only_whitelist_panel(database: Database) -> None:
    regular = FakePanel(
        clients=[
            make_panel_client("alice", [1], up=100, down=200),
            make_panel_client("stranger", [1], up=9_000, down=9_000),
        ],
    )
    whitelist = FakePanel(
        clients=[
            make_panel_client("alice", [2], up=10, down=20),
            make_panel_client("stranger", [2], up=8_000, down=8_000),
        ],
    )
    subscription = await make_subscription(database, panels=[regular], whitelist_panel=whitelist)
    (await create_alice(database))

    assert (await subscription.bandwidth_svc.bandwidth("alice")) == BandwidthInfo(100, 200, 300)
    assert (await subscription.bandwidth_svc.bandwidth("alice", whitelist=True)) == BandwidthInfo(
        10,
        20,
        30,
    )
    assert (await subscription.bandwidth_svc.bandwidth("alice")).total == 300


@pytest.mark.asyncio
async def test_whitelist_total_is_zero_without_whitelist_panel(database: Database) -> None:
    regular = FakePanel(clients=[make_panel_client("alice", [1], up=100, down=200)])
    subscription = await make_subscription(database, panels=[regular], whitelist_panel=None)
    (await create_alice(database))

    assert (await subscription.bandwidth_svc.bandwidth("alice")) == BandwidthInfo(100, 200, 300)
    assert (await subscription.bandwidth_svc.bandwidth("alice", whitelist=True)) == BandwidthInfo(
        0,
        0,
        0,
    )


@pytest.mark.asyncio
async def test_traffic_is_one_shared_row_not_summed_over_inbounds(database: Database) -> None:
    """Double-count regression: a client attached to 2 inbounds has ONE
    ``client_traffics`` row; reading it must not multiply it by inbound count."""
    panel = FakePanel(clients=[make_panel_client("alice", [1, 2], up=5, down=6)])
    subscription = await make_subscription(database, panels=[panel])
    (await create_alice(database))

    assert (await subscription.bandwidth_svc.bandwidth("alice")) == BandwidthInfo(5, 6, 11)


@pytest.mark.asyncio
@pytest.mark.parametrize("whitelist", [False, True])
async def test_individual_traffic_omits_foreign_and_missing_rows(
    database: Database,
    whitelist: bool,
) -> None:
    owned = FakePanel(name="owned", clients=[make_panel_client("alice", [1], up=5, down=6)])
    foreign = FakePanel(
        name="foreign", clients=[make_panel_client("alice", [1], up=900, down=800, uuid=NEW_UUID)]
    )
    missing = FakePanel(name="missing")
    if whitelist:
        subscription = await make_subscription(
            database, panels=[owned, missing], whitelist_panel=foreign
        )
    else:
        subscription = await make_subscription(database, panels=[owned, foreign, missing])
    await create_alice(database)

    assert await subscription.bandwidth_svc.bandwidth("alice") == BandwidthInfo(5, 6, 11)
    if whitelist:
        assert await subscription.bandwidth_svc.bandwidth("alice", whitelist=True) == BandwidthInfo(
            0, 0, 0
        )


@pytest.mark.asyncio
async def test_individual_traffic_omits_wrong_email(database: Database) -> None:
    client = make_panel_client("alice", [1], up=5, down=6)
    assert client.traffic is not None
    panel = FakePanel(clients=[replace(client, traffic=replace(client.traffic, email="bob"))])
    subscription = await make_subscription(database, panels=[panel])
    await create_alice(database)

    assert await subscription.bandwidth_svc.bandwidth("alice") == BandwidthInfo(0, 0, 0)


@pytest.mark.asyncio
async def test_individual_traffic_propagates_panel_failure(database: Database) -> None:
    owned = FakePanel(name="owned", clients=[make_panel_client("alice", [1], up=5, down=6)])
    offline = FakePanel(name="offline", clients=[make_panel_client("alice", [1])], get_status=503)
    subscription = await make_subscription(database, panels=[owned, offline])
    await create_alice(database)

    with pytest.raises(PanelUnavailableError):
        await subscription.bandwidth_svc.bandwidth("alice")


@pytest.mark.asyncio
@pytest.mark.parametrize("row_uuid", [USER_UUID, NEW_UUID])
async def test_individual_traffic_uses_identity_after_concurrent_uuid_change(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    row_uuid: str,
) -> None:
    panel = FakePanel(clients=[make_panel_client("alice", [1], up=5, down=6)])
    subscription = await make_subscription(database, panels=[panel])
    await create_alice(database)
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_traffic(panel: XUiSession, email: str) -> ClientTraffic:
        assert email == "alice"
        started.set()
        await release.wait()
        return make_client(row_uuid, 5, 6)

    monkeypatch.setattr(subscription.panel_svc, "client_traffic", delayed_traffic)
    pending = asyncio.create_task(subscription.bandwidth_svc.bandwidth("alice"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.wait_for(subscription.business_svc.update_uuid("alice", NEW_UUID), timeout=5)
    finally:
        release.set()
    expected = BandwidthInfo(5, 6, 11) if row_uuid == NEW_UUID else BandwidthInfo(0, 0, 0)
    assert await asyncio.wait_for(pending, timeout=5) == expected


@pytest.mark.asyncio
async def test_individual_traffic_omits_rows_after_concurrent_deletion(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subscription = await make_subscription(database)
    await create_alice(database)
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_traffic(panel: XUiSession, email: str) -> ClientTraffic:
        started.set()
        await release.wait()
        return make_client(USER_UUID, 5, 6)

    monkeypatch.setattr(subscription.panel_svc, "client_traffic", delayed_traffic)
    pending = asyncio.create_task(subscription.bandwidth_svc.bandwidth("alice"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await database.delete_user("alice")
    finally:
        release.set()
    assert await asyncio.wait_for(pending, timeout=5) == BandwidthInfo(0, 0, 0)


@pytest.mark.asyncio
async def test_individual_traffic_does_not_deadlock_under_user_mutation_lock(
    database: Database,
) -> None:
    panel = FakePanel(clients=[make_panel_client("alice", [1], up=5, down=6)])
    subscription = await make_subscription(database, panels=[panel])
    await create_alice(database)

    async def locked_info() -> BandwidthInfo:
        info = await subscription.business_svc.get_info("alice")
        return BandwidthInfo(
            info.bandwidth.total.upload, info.bandwidth.total.download, info.bandwidth.total.total
        )

    result = await asyncio.wait_for(
        subscription.res.coordinator.run(["alice"], locked_info, name="test:locked-info"), timeout=5
    )
    assert result == BandwidthInfo(5, 6, 11)


@pytest.mark.asyncio
async def test_all_traffic_sums_across_panels_and_skips_unknown_emails(
    database: Database,
) -> None:
    one = FakePanel(
        name="one",
        clients=[
            make_panel_client("alice", [1], up=100, down=200),
            make_panel_client("stranger", [1], up=9_000, down=9_000),
        ],
    )
    two = FakePanel(
        name="two",
        clients=[
            make_panel_client("alice", [2], up=1, down=2),
        ],
    )
    subscription = await make_subscription(database, panels=[one, two])
    (await create_alice(database))

    traffic = await subscription.bandwidth_svc.all_traffic()
    assert traffic == {"alice": BandwidthInfo(101, 202, 303)}
    assert await subscription.bandwidth_svc.panel_traffic() == {
        "one": {"alice": BandwidthInfo(100, 200, 300)},
        "two": {"alice": BandwidthInfo(1, 2, 3)},
    }
    assert (await subscription.bandwidth_svc.all_traffic(whitelist=True)) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("whitelist", [False, True])
@pytest.mark.parametrize("include_owned", [False, True])
async def test_traffic_rejects_foreign_uuid_under_known_email(
    database: Database,
    whitelist: bool,
    include_owned: bool,
) -> None:
    clients = [make_panel_client("alice", [1], up=5, down=6)] if include_owned else []
    clients.append(
        make_panel_client(
            "alice",
            [2],
            up=9_000,
            down=9_000,
            uuid="ffffffff-ffff-ffff-ffff-ffffffffffff",
        ),
    )
    panel = FakePanel(name="target", clients=clients)
    subscription = await make_subscription(
        database,
        panels=[] if whitelist else [panel],
        whitelist_panel=panel if whitelist else None,
    )
    await create_alice(database)
    expected = {"alice": BandwidthInfo(5, 6, 11)} if include_owned else {}

    assert await subscription.bandwidth_svc.panel_traffic(whitelist=whitelist) == {
        "target": expected,
    }
    assert await subscription.bandwidth_svc.all_traffic(whitelist=whitelist) == expected


@pytest.mark.asyncio
async def test_all_traffic_raises_when_no_panel_queryable(database: Database) -> None:
    one = FakePanel(name="one", get_error=RuntimeError("down"))
    two = FakePanel(name="two", get_error=RuntimeError("down"))
    subscription = await make_subscription(database, panels=[one, two])
    (await create_alice(database))

    with pytest.raises(PanelUnavailableError):
        (await subscription.bandwidth_svc.all_traffic())


@pytest.mark.asyncio
async def test_all_traffic_rejects_partial_when_one_panel_fails(database: Database) -> None:
    one = FakePanel(name="one", get_error=RuntimeError("down"))
    two = FakePanel(name="two", clients=[make_panel_client("alice", [2], up=7, down=8)])
    subscription = await make_subscription(database, panels=[one, two])
    (await create_alice(database))

    with pytest.raises(PanelUnavailableError):
        await subscription.bandwidth_svc.all_traffic()

    assert await subscription.bandwidth_svc.panel_traffic() == {
        "two": {"alice": BandwidthInfo(7, 8, 15)},
    }


@pytest.mark.asyncio
async def test_all_traffic_uses_whitelist_slot(database: Database) -> None:
    regular = FakePanel(
        name="regular",
        clients=[
            make_panel_client("alice", [1], up=100, down=200),
        ],
    )
    whitelist = FakePanel(
        name="whitelist",
        clients=[
            make_panel_client("alice", [2], up=10, down=20),
        ],
    )
    subscription = await make_subscription(
        database,
        panels=[regular],
        whitelist_panel=whitelist,
    )
    (await create_alice(database))

    assert (await subscription.bandwidth_svc.all_traffic(whitelist=True)) == {
        "alice": BandwidthInfo(10, 20, 30),
    }
    # the whitelist panel is excluded from the main map
    assert (await subscription.bandwidth_svc.all_traffic()) == {
        "alice": BandwidthInfo(100, 200, 300),
    }


@pytest.mark.asyncio
async def test_get_info_keeps_raw_whitelist_bytes(database: Database) -> None:
    regular = FakePanel(clients=[make_panel_client("alice", [1], up=100, down=200)])
    whitelist = FakePanel(clients=[make_panel_client("alice", [2], up=10, down=20)])
    subscription = await make_subscription(database, panels=[regular], whitelist_panel=whitelist)
    (await create_alice(database))
    info = await subscription.business_svc.get_info("alice", pretty=False)
    pretty = await subscription.business_svc.get_info("alice", pretty=True)
    assert info.bandwidth.wl_total.total == 30
    assert pretty.bandwidth.wl_total.total == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("monthly", "wl_monthly", "expected_monthly", "expected_wl_monthly"),
    [(0, 0, 0.0, 0.0), (1_000_000_000, 2_345_678_901, 1000.0, 2345.68)],
)
async def test_get_info_monthly_units_follow_pretty(
    database: Database,
    monthly: int,
    wl_monthly: int,
    expected_monthly: float,
    expected_wl_monthly: float,
) -> None:
    subscription = await make_subscription(database)
    await create_alice(database)
    await database.update_user(
        "alice",
        bw_used=monthly,
        wl_bw_used=wl_monthly,
        bw_limit=5,
        wl_bw_limit=10,
    )

    for info in (
        await subscription.business_svc.get_info("alice"),
        await subscription.business_svc.get_info("alice", pretty=False),
    ):
        assert info.bandwidth.monthly == monthly
        assert info.bandwidth.wl_monthly == wl_monthly
        assert isinstance(info.bandwidth.monthly, int)
        assert isinstance(info.bandwidth.wl_monthly, int)

    pretty = await subscription.business_svc.get_info("alice", pretty=True)
    assert pretty.bandwidth.monthly == expected_monthly
    assert pretty.bandwidth.wl_monthly == expected_wl_monthly
    assert pretty.bandwidth.limit == 5
    assert pretty.bandwidth.wl_limit == 10
    user = await subscription.user_svc.user("alice")
    assert user["bw_used"] == monthly
    assert user["wl_used"] == wl_monthly


@pytest.mark.asyncio
async def test_get_info_fetches_traffic_and_online_concurrently(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subscription = await make_subscription(database)
    await create_alice(database)
    started: set[str] = set()
    all_started = asyncio.Event()

    async def rendezvous(name: str) -> None:
        started.add(name)
        if len(started) == 3:
            all_started.set()
        await all_started.wait()

    async def bandwidth(username: str, whitelist: bool = False) -> BandwidthInfo:
        assert username == "alice"
        await rendezvous("whitelist" if whitelist else "main")
        return BandwidthInfo(10, 20, 30) if whitelist else BandwidthInfo(100, 200, 300)

    async def is_online(username: str) -> bool:
        assert username == "alice"
        await rendezvous("online")
        return True

    monkeypatch.setattr(subscription.bandwidth_svc, "bandwidth", bandwidth)
    monkeypatch.setattr(subscription.panel_svc, "is_online", is_online)

    info = await asyncio.wait_for(subscription.business_svc.get_info("alice"), timeout=5)

    assert info.bandwidth.total.total == 300
    assert info.bandwidth.wl_total.total == 30
    assert info.online is True
