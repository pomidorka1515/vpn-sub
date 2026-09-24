from __future__ import annotations

from typing import cast

from custom_types import BandwidthInfo
from db import Database
from helpers import (
    FakePanel,
    create_alice,
    make_panel_client,
    make_subscription,
)
from session import XUiSession


def test_whitelist_total_uses_only_whitelist_panel(database: Database) -> None:
    regular = FakePanel(clients=[
        make_panel_client("alice", [1], up=100, down=200),
        make_panel_client("stranger", [1], up=9_000, down=9_000),
    ])
    whitelist = FakePanel(clients=[
        make_panel_client("alice", [2], up=10, down=20),
        make_panel_client("stranger", [2], up=8_000, down=8_000),
    ])
    subscription = make_subscription(database, panels=[regular], whitelist_panel=whitelist)
    create_alice(database)

    assert subscription.bandwidth_svc.bandwidth("alice") == BandwidthInfo(100, 200, 300)
    assert subscription.bandwidth_svc.bandwidth("alice", whitelist=True) == BandwidthInfo(10, 20, 30)
    assert subscription.bandwidth_svc.bandwidth("alice").total == 300


def test_whitelist_total_is_zero_without_whitelist_panel(database: Database) -> None:
    regular = FakePanel(clients=[make_panel_client("alice", [1], up=100, down=200)])
    subscription = make_subscription(database, panels=[regular], whitelist_panel=None)
    create_alice(database)

    assert subscription.bandwidth_svc.bandwidth("alice") == BandwidthInfo(100, 200, 300)
    assert subscription.bandwidth_svc.bandwidth("alice", whitelist=True) == BandwidthInfo(0, 0, 0)


def test_traffic_is_one_shared_row_not_summed_over_inbounds(database: Database) -> None:
    """Double-count regression: a client attached to 2 inbounds has ONE
    ``client_traffics`` row; reading it must not multiply it by inbound count."""
    panel = FakePanel(clients=[make_panel_client("alice", [1, 2], up=5, down=6)])
    subscription = make_subscription(database, panels=[cast(XUiSession, panel)])
    create_alice(database)

    assert subscription.bandwidth_svc.bandwidth("alice") == BandwidthInfo(5, 6, 11)


def test_all_traffic_sums_across_panels_and_skips_unknown_emails(
    database: Database,
) -> None:
    one = FakePanel(name="one", clients=[
        make_panel_client("alice", [1], up=100, down=200),
        make_panel_client("stranger", [1], up=9_000, down=9_000),
    ])
    two = FakePanel(name="two", clients=[
        make_panel_client("alice", [2], up=1, down=2),
    ])
    subscription = make_subscription(database, panels=[one, two])
    create_alice(database)

    traffic = subscription.bandwidth_svc.all_traffic()
    assert traffic == {"alice": BandwidthInfo(101, 202, 303)}
    assert subscription.bandwidth_svc.all_traffic(whitelist=True) == {}


def test_all_traffic_raises_when_no_panel_queryable(database: Database) -> None:
    one = FakePanel(name="one", get_error=RuntimeError("down"))
    two = FakePanel(name="two", get_error=RuntimeError("down"))
    subscription = make_subscription(database, panels=[one, two])
    create_alice(database)

    from errors import PanelUnavailableError
    import pytest
    with pytest.raises(PanelUnavailableError):
        subscription.bandwidth_svc.all_traffic()


def test_all_traffic_returns_partial_when_one_panel_fails(database: Database) -> None:
    one = FakePanel(name="one", get_error=RuntimeError("down"))
    two = FakePanel(name="two", clients=[make_panel_client("alice", [2], up=7, down=8)])
    subscription = make_subscription(database, panels=[one, two])
    create_alice(database)

    traffic = subscription.bandwidth_svc.all_traffic()
    assert traffic == {"alice": BandwidthInfo(7, 8, 15)}


def test_all_traffic_uses_whitelist_slot(database: Database) -> None:
    regular = FakePanel(name="regular", clients=[
        make_panel_client("alice", [1], up=100, down=200),
    ])
    whitelist = FakePanel(name="whitelist", clients=[
        make_panel_client("alice", [2], up=10, down=20),
    ])
    subscription = make_subscription(
        database, panels=[regular], whitelist_panel=whitelist,
    )
    create_alice(database)

    assert subscription.bandwidth_svc.all_traffic(whitelist=True) == {
        "alice": BandwidthInfo(10, 20, 30),
    }
    # the whitelist panel is excluded from the main map
    assert subscription.bandwidth_svc.all_traffic() == {
        "alice": BandwidthInfo(100, 200, 300),
    }


def test_get_info_keeps_raw_whitelist_bytes(database: Database) -> None:
    regular = FakePanel(clients=[make_panel_client("alice", [1], up=100, down=200)])
    whitelist = FakePanel(clients=[make_panel_client("alice", [2], up=10, down=20)])
    subscription = make_subscription(database, panels=[regular], whitelist_panel=whitelist)
    create_alice(database)
    subscription.panel_svc.is_online = lambda username: False  # type: ignore[method-assign]
    info = subscription.business_svc.get_info("alice", pretty=False)
    pretty = subscription.business_svc.get_info("alice", pretty=True)
    assert info.bandwidth.wl_total.total == 30
    assert pretty.bandwidth.wl_total.total == 0.0
