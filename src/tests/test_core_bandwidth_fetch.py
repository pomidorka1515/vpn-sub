from __future__ import annotations

from custom_types import BandwidthInfo
from db import Database
from helpers import (
    USER_UUID,
    FakePanel,
    create_alice,
    make_client,
    make_inbound,
    make_subscription,
)


def test_whitelist_total_uses_only_whitelist_panel(database: Database) -> None:
    other_uuid = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    regular = FakePanel(inbounds=[
        make_inbound(1, [
            make_client(USER_UUID, up=100, down=200),
            make_client(other_uuid, up=9_000, down=9_000),
        ]),
    ])
    whitelist = FakePanel(inbounds=[
        make_inbound(2, [
            make_client(USER_UUID, up=10, down=20, inbound_id=2),
            make_client(other_uuid, up=8_000, down=8_000, inbound_id=2),
        ]),
    ])
    subscription = make_subscription(database, panels=[regular], whitelist_panel=whitelist)
    create_alice(database)

    assert subscription.bandwidth_svc.bandwidth("alice") == BandwidthInfo(100, 200, 300)
    assert subscription.bandwidth_svc.bandwidth("alice", whitelist=True) == BandwidthInfo(10, 20, 30)


def test_whitelist_total_is_zero_without_whitelist_panel(database: Database) -> None:
    regular = FakePanel(inbounds=[make_inbound(1, [make_client(USER_UUID, up=100, down=200)])])
    subscription = make_subscription(database, panels=[regular], whitelist_panel=None)
    create_alice(database)

    assert subscription.bandwidth_svc.bandwidth("alice") == BandwidthInfo(100, 200, 300)
    assert subscription.bandwidth_svc.bandwidth("alice", whitelist=True) == BandwidthInfo(0, 0, 0)


def test_get_info_keeps_raw_whitelist_bytes(database: Database) -> None:
    regular = FakePanel(inbounds=[make_inbound(1, [make_client(USER_UUID, up=100, down=200)])])
    whitelist = FakePanel(inbounds=[make_inbound(2, [make_client(USER_UUID, up=10, down=20, inbound_id=2)])])
    subscription = make_subscription(database, panels=[regular], whitelist_panel=whitelist)
    create_alice(database)
    subscription.panel_svc.is_online = lambda username: False  # type: ignore[method-assign]
    info = subscription.business_svc.get_info("alice", pretty=False)
    pretty = subscription.business_svc.get_info("alice", pretty=True)
    assert info.bandwidth.wl_total.total == 30
    assert pretty.bandwidth.wl_total.total == 0.0
