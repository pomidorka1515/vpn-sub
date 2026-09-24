from __future__ import annotations

from dataclasses import asdict
from typing import Any, cast

import pytest

from core import Subscription
from db import Database
from errors import PanelRejectedError, PanelUnavailableError
from helpers import (
    USER_UUID,
    FakePanel,
    create_alice,
    make_panel_client,
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


def test_get_client_parses_panel_client(
    database: Database, subscription: Subscription,
) -> None:
    create_alice(database)
    panel = FakePanel(clients=[make_panel_client("alice", [1, 2], up=3, down=4)])
    client = subscription.panel_svc.get_client(cast(XUiSession, panel), "alice")
    assert client is not None
    assert client.email == "alice"
    assert client.uuid == USER_UUID
    assert client.inboundIds == [1, 2]
    assert client.traffic is not None
    assert (client.traffic.up, client.traffic.down) == (3, 4)
    assert panel.gets[-1] == "panel/api/clients/get/alice"


def test_get_client_returns_none_when_not_found(
    database: Database, subscription: Subscription,
) -> None:
    create_alice(database)
    panel = FakePanel(clients=[])
    assert subscription.panel_svc.get_client(cast(XUiSession, panel), "alice") is None
    assert panel.gets[-1] == "panel/api/clients/get/alice"


def test_get_client_raises_unavailable_on_404(
    subscription: Subscription,
) -> None:
    panel = FakePanel(
        get_payload={"success": True, "msg": "", "obj": None},
        get_status=404,
    )
    with pytest.raises(PanelUnavailableError):
        subscription.panel_svc.get_client(cast(XUiSession, panel), "alice")


def test_client_traffic_raises_unavailable_on_404(
    subscription: Subscription,
) -> None:
    panel = FakePanel(
        get_payload={"success": True, "msg": "", "obj": None},
        get_status=404,
    )
    with pytest.raises(PanelUnavailableError):
        subscription.panel_svc.client_traffic(cast(XUiSession, panel), "alice")


def test_get_client_raises_unavailable_when_panel_is_down(
    subscription: Subscription,
) -> None:
    panel = FakePanel(
        get_payload={"success": False, "msg": "Panel panel is down: unavailable", "obj": None},
        get_status=503,
    )
    with pytest.raises(PanelUnavailableError, match="down"):
        subscription.panel_svc.get_client(cast(XUiSession, panel), "alice")


def test_client_traffic_raises_unavailable_when_panel_is_down(
    subscription: Subscription,
) -> None:
    panel = FakePanel(
        get_payload={"success": False, "msg": "Panel panel is down: unavailable", "obj": None},
        get_status=503,
    )
    with pytest.raises(PanelUnavailableError, match="down"):
        subscription.panel_svc.client_traffic(cast(XUiSession, panel), "alice")


def test_get_client_quotes_email_in_path(
    subscription: Subscription,
) -> None:
    panel = FakePanel(clients=[])
    client = subscription.panel_svc.get_client(cast(XUiSession, panel), "al/ice?x")
    assert client is None
    assert panel.gets[-1] == "panel/api/clients/get/al%2Fice%3Fx"


def test_client_traffic_quotes_email_in_path(
    subscription: Subscription,
) -> None:
    panel = FakePanel(clients=[])
    traffic = subscription.panel_svc.client_traffic(cast(XUiSession, panel), "al/ice?x")
    assert traffic is None
    assert panel.gets[-1] == "panel/api/clients/traffic/al%2Fice%3Fx"


def test_get_client_raises_with_panel_msg_on_rejection(
    subscription: Subscription,
) -> None:
    panel = FakePanel(
        get_payload={"success": False, "msg": "database is locked", "obj": None},
    )
    with pytest.raises(PanelRejectedError, match="database is locked"):
        subscription.panel_svc.get_client(cast(XUiSession, panel), "alice")


def test_client_traffic_returns_single_shared_row(
    database: Database, subscription: Subscription,
) -> None:
    create_alice(database)
    panel = FakePanel(clients=[make_panel_client("alice", [1, 2], up=5, down=6)])
    traffic = subscription.panel_svc.client_traffic(cast(XUiSession, panel), "alice")
    assert traffic is not None
    assert (traffic.up, traffic.down) == (5, 6)
    assert panel.gets[-1] == "panel/api/clients/traffic/alice"


def test_client_traffic_returns_none_when_not_found(
    database: Database, subscription: Subscription,
) -> None:
    create_alice(database)
    panel = FakePanel(clients=[])
    traffic = subscription.panel_svc.client_traffic(cast(XUiSession, panel), "alice")
    assert traffic is None
    assert panel.gets[-1] == "panel/api/clients/traffic/alice"


def test_list_clients_parses_attachments(
    database: Database, subscription: Subscription,
) -> None:
    create_alice(database)
    panel = FakePanel(clients=[
        make_panel_client("alice", [1, 2], up=1, down=2),
        make_panel_client("bob", [2], up=3, down=4),
    ])
    clients = subscription.panel_svc.list_clients(cast(XUiSession, panel))
    assert [c.email for c in clients] == ["alice", "bob"]
    assert clients[0].inboundIds == [1, 2]
    assert clients[1].traffic is not None
    assert (clients[1].traffic.up, clients[1].traffic.down) == (3, 4)
    assert panel.gets[-1] == "panel/api/clients/list"


def test_list_clients_parses_numeric_tg_id(
    subscription: Subscription,
) -> None:
    client = asdict(make_panel_client("alice", [1], up=1, down=2))
    client["tgId"] = 123
    panel = FakePanel(get_payload={"success": True, "msg": "", "obj": [client]})
    clients = subscription.panel_svc.list_clients(cast(XUiSession, panel))
    assert clients[0].tgId == 123


def test_get_client_parses_numeric_tg_id(
    subscription: Subscription,
) -> None:
    client = asdict(make_panel_client("alice", [1], up=1, down=2))
    client["tgId"] = 123
    panel = FakePanel(get_payload={"success": True, "msg": "", "obj": client})
    found = subscription.panel_svc.get_client(cast(XUiSession, panel), "alice")
    assert found is not None
    assert found.tgId == 123


def test_is_online_requires_healthy_panel(
    database: Database,
) -> None:
    create_alice(database)
    panel = FakePanel(
        name="panel",
        post_payload={"success": True, "obj": ["alice"]},
    )
    subscription = make_subscription(database, panels=[panel])
    assert subscription.panel_svc.is_online("alice")
    assert subscription.panel_svc.get_online_users() == ["alice"]
    assert subscription.panel_svc.get_online_users(new=True) == {"alice": None}
    assert panel.posts[0][0] == "panel/api/clients/onlines"


def test_is_online_false_without_panels(subscription: Subscription) -> None:
    assert subscription.panel_svc.is_online("alice") is False
