from __future__ import annotations

from typing import Any, cast

from core import Subscription
from db import Database
from helpers import (
    USER_UUID,
    FakePanel,
    create_alice,
    make_client,
    make_inbound,
    make_subscription,
)
from session import XUiSession


def _status_obj() -> dict[str, Any]:
    return {
        "cpu": 1.234,
        "cpuCores": 2,
        "logicalPro": 2,
        "cpuSpeedMhz": 2400.9,
        "mem": {"current": 1, "total": 2},
        "swap": {"current": 0, "total": 0},
        "disk": {"current": 3, "total": 4},
        "xray": {"state": "running", "errorMsg": "", "version": "1"},
        "uptime": 10,
        "loads": [0.1, 0.2, 0.3],
        "tcpCount": 1,
        "udpCount": 2,
        "netIO": {"up": 1, "down": 2},
        "netTraffic": {"sent": 3, "recv": 4},
        "publicIP": {"ipv4": "1.1.1.1", "ipv6": "::1"},
        "appStats": {"threads": 1, "mem": 2, "uptime": 3},
    }


def test_getstatus_returns_metrics(subscription: Subscription) -> None:
    panel = FakePanel(
        name="panel",
        status_payload={"success": True, "msg": "", "obj": _status_obj()},
    )
    status = subscription.panel_svc.getstatus(cast(XUiSession, panel))
    assert status is not None
    assert status.obj.cpuCores == 2
    assert "server/status" in panel.gets[-1]


def test_getstatus_returns_none_on_failure(subscription: Subscription) -> None:
    panel = FakePanel(name="panel", status_error=RuntimeError("down"))
    assert subscription.panel_svc.getstatus(cast(XUiSession, panel)) is None


def test_get_emails_parses_username_prefix(
    database: Database, subscription: Subscription,
) -> None:
    create_alice(database)
    panel = FakePanel(inbounds=[
        make_inbound(7, [make_client(USER_UUID, up=1, down=1, inbound_id=7)]),
    ])
    emails = subscription.panel_svc.get_emails("alice", cast(XUiSession, panel))
    assert emails == {"7": "alice-abcd1234"}


def test_is_online_requires_healthy_panel(
    database: Database,
) -> None:
    create_alice(database)
    panel = FakePanel(
        name="panel",
        post_payload={"success": True, "obj": ["alice-abcd1234"]},
    )
    subscription = make_subscription(database, panels=[panel])
    assert subscription.panel_svc.is_online("alice")
    assert subscription.panel_svc.get_online_users() == ["alice"]
    assert subscription.panel_svc.get_online_users(new=True) == {"alice": None}


def test_is_online_false_without_panels(subscription: Subscription) -> None:
    assert subscription.panel_svc.is_online("alice") is False
