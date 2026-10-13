from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from helpers import (
    FakePanel,
    create_alice,
    make_panel_client,
    make_subscription,
)

from custom_types import BandwidthInfo
from session import XUiSession

if TYPE_CHECKING:
    from db import Database


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
    subscription = (await make_subscription(database, panels=[regular], whitelist_panel=whitelist))
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
    subscription = (await make_subscription(database, panels=[regular], whitelist_panel=None))
    (await create_alice(database))

    assert (await subscription.bandwidth_svc.bandwidth("alice")) == BandwidthInfo(100, 200, 300)
    assert (await subscription.bandwidth_svc.bandwidth("alice", whitelist=True)) == BandwidthInfo(0, 0, 0)


@pytest.mark.asyncio
async def test_traffic_is_one_shared_row_not_summed_over_inbounds(database: Database) -> None:
    """Double-count regression: a client attached to 2 inbounds has ONE
    ``client_traffics`` row; reading it must not multiply it by inbound count."""
    panel = FakePanel(clients=[make_panel_client("alice", [1, 2], up=5, down=6)])
    subscription = (await make_subscription(database, panels=[cast(XUiSession, panel)]))
    (await create_alice(database))

    assert (await subscription.bandwidth_svc.bandwidth("alice")) == BandwidthInfo(5, 6, 11)


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
    subscription = (await make_subscription(database, panels=[one, two]))
    (await create_alice(database))

    traffic = (await subscription.bandwidth_svc.all_traffic())
    assert traffic == {"alice": BandwidthInfo(101, 202, 303)}
    assert (await subscription.bandwidth_svc.all_traffic(whitelist=True)) == {}


@pytest.mark.asyncio
async def test_all_traffic_raises_when_no_panel_queryable(database: Database) -> None:
    one = FakePanel(name="one", get_error=RuntimeError("down"))
    two = FakePanel(name="two", get_error=RuntimeError("down"))
    subscription = (await make_subscription(database, panels=[one, two]))
    (await create_alice(database))

    from errors import PanelUnavailableError

    with pytest.raises(PanelUnavailableError):
        (await subscription.bandwidth_svc.all_traffic())


@pytest.mark.asyncio
async def test_all_traffic_returns_partial_when_one_panel_fails(database: Database) -> None:
    one = FakePanel(name="one", get_error=RuntimeError("down"))
    two = FakePanel(name="two", clients=[make_panel_client("alice", [2], up=7, down=8)])
    subscription = (await make_subscription(database, panels=[one, two]))
    (await create_alice(database))

    traffic = (await subscription.bandwidth_svc.all_traffic())
    assert traffic == {"alice": BandwidthInfo(7, 8, 15)}


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
    subscription = (await make_subscription(
        database,
        panels=[regular],
        whitelist_panel=whitelist,
    ))
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
    subscription = (await make_subscription(database, panels=[regular], whitelist_panel=whitelist))
    (await create_alice(database))
    info = (await subscription.business_svc.get_info("alice", pretty=False))
    pretty = (await subscription.business_svc.get_info("alice", pretty=True))
    assert info.bandwidth.wl_total.total == 30
    assert pretty.bandwidth.wl_total.total == 0.0
