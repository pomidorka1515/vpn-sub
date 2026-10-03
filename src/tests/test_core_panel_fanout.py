# Panel poll fan-out: overlap, order, and first-failure contracts.
from __future__ import annotations

from typing import Any, cast
import threading
import time
import uuid

import pytest

from db import Database
from errors import PanelUnavailableError
from helpers import (
    USER_UUID,
    FakePanel,
    create_alice,
    make_panel_client,
    make_subscription,
)
from requests import Response
from session import XUiSession


def _status_obj() -> dict[str, Any]:
    return {
        "cpu": 1.0,
        "cpuCores": 2,
        "logicalPro": 2,
        "cpuSpeedMhz": 2400.0,
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


class _Span:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.starts: list[float] = []
        self.ends: list[float] = []

    def overlapped(self) -> bool:
        with self.lock:
            return max(self.starts) < min(self.ends)


class TimedPanel(FakePanel):
    """Records start/end of get/post under a shared lock, then sleeps."""

    def __init__(self, span: _Span, delay: float, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._span = span
        self._delay = delay

    def _mark(self) -> None:
        with self._span.lock:
            self._span.starts.append(time.monotonic())
        time.sleep(self._delay)
        with self._span.lock:
            self._span.ends.append(time.monotonic())

    def get(self, url: str) -> Response:
        self._mark()
        return super().get(url)

    def post(self, url: str, **kwargs: object) -> Response:
        self._mark()
        return super().post(url, **kwargs)


def test_polls_overlap(database: Database) -> None:
    span = _Span()
    one = TimedPanel(
        span, 0.05, name="one",
        post_payload={"success": True, "obj": []},
        status_payload={"success": True, "msg": "", "obj": _status_obj()},
    )
    two = TimedPanel(
        span, 0.05, name="two",
        post_payload={"success": True, "obj": []},
        status_payload={"success": True, "msg": "", "obj": _status_obj()},
    )
    subscription = make_subscription(
        database, panels=[cast(XUiSession, one), cast(XUiSession, two)],
    )
    create_alice(database)

    subscription.panel_svc.get_online_status()
    assert span.overlapped()

    span.starts.clear()
    span.ends.clear()
    subscription.bandwidth_svc.all_traffic()
    assert span.overlapped()

    span.starts.clear()
    span.ends.clear()
    statuses = subscription.panel_svc.statuses()
    assert span.overlapped()
    assert len(statuses) == 2
    assert statuses[0] is not None and statuses[1] is not None


def test_online_users_follow_panel_list_order(database: Database) -> None:
    span = _Span()
    slow = TimedPanel(
        span, 0.05, name="slow",
        post_payload={"success": True, "obj": ["bob"]},
    )
    fast = TimedPanel(
        span, 0.0, name="fast",
        post_payload={"success": True, "obj": ["alice"]},
    )
    subscription = make_subscription(
        database, panels=[cast(XUiSession, slow), cast(XUiSession, fast)],
    )
    database.create_user(
        username="bob", uuid=str(uuid.uuid4()), token="b" * 40,
        fingerprint="chrome", displayname="Bob",
    )
    create_alice(database)

    status = subscription.panel_svc.get_online_status()
    assert status.users == ["bob", "alice"]
    assert list(status.panel_health) == ["slow", "fast"]


def test_bandwidth_raises_from_first_panel(database: Database) -> None:
    span = _Span()
    slow = TimedPanel(span, 0.05, name="one", get_error=RuntimeError("one"))
    fast = TimedPanel(span, 0.0, name="two", get_error=RuntimeError("two"))
    subscription = make_subscription(
        database, panels=[cast(XUiSession, slow), cast(XUiSession, fast)],
    )
    create_alice(database)

    with pytest.raises(PanelUnavailableError, match="one"):
        subscription.bandwidth_svc.bandwidth("alice")


def test_all_traffic_keeps_fast_panel_when_slow_raises(database: Database) -> None:
    span = _Span()
    slow = TimedPanel(span, 0.05, name="slow", get_error=RuntimeError("down"))
    fast = TimedPanel(
        span, 0.0, name="fast",
        clients=[make_panel_client("alice", [1], up=7, down=8)],
    )
    subscription = make_subscription(
        database, panels=[cast(XUiSession, slow), cast(XUiSession, fast)],
    )
    create_alice(database)

    from custom_types import BandwidthInfo
    assert subscription.bandwidth_svc.all_traffic() == {
        "alice": BandwidthInfo(7, 8, 15),
    }
