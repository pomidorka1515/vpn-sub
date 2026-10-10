# Panel poll fan-out: overlap, order, and first-failure contracts.
from __future__ import annotations

import threading
import time
import uuid
from typing import TYPE_CHECKING, Unpack, cast

import pytest
from helpers import (
    FakePanel,
    create_alice,
    make_inbound,
    make_panel_client,
    make_subscription,
)

from errors import PanelRejectedError, PanelUnavailableError
from session import XUiSession

if TYPE_CHECKING:
    from requests import Response
    from typing_contracts import FakePanelOptions

    from config import JsonValue
    from db import Database


def _status_obj() -> dict[str, JsonValue]:
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

    def __init__(self, span: _Span, delay: float, **kwargs: Unpack[FakePanelOptions]) -> None:
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
        span,
        0.05,
        name="one",
        post_payload={"success": True, "obj": []},
        status_payload={"success": True, "msg": "", "obj": _status_obj()},
    )
    two = TimedPanel(
        span,
        0.05,
        name="two",
        post_payload={"success": True, "obj": []},
        status_payload={"success": True, "msg": "", "obj": _status_obj()},
    )
    subscription = make_subscription(
        database,
        panels=[cast(XUiSession, one), cast(XUiSession, two)],
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
    assert statuses[0] is not None
    assert statuses[1] is not None


def test_online_users_follow_panel_list_order(database: Database) -> None:
    span = _Span()
    slow = TimedPanel(
        span,
        0.05,
        name="slow",
        post_payload={"success": True, "obj": ["bob"]},
    )
    fast = TimedPanel(
        span,
        0.0,
        name="fast",
        post_payload={"success": True, "obj": ["alice"]},
    )
    subscription = make_subscription(
        database,
        panels=[cast(XUiSession, slow), cast(XUiSession, fast)],
    )
    database.create_user(
        username="bob",
        uuid=str(uuid.uuid4()),
        token="b" * 40,
        fingerprint="chrome",
        displayname="Bob",
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
        database,
        panels=[cast(XUiSession, slow), cast(XUiSession, fast)],
    )
    create_alice(database)

    with pytest.raises(PanelUnavailableError, match="one"):
        subscription.bandwidth_svc.bandwidth("alice")


def test_all_traffic_keeps_fast_panel_when_slow_raises(database: Database) -> None:
    span = _Span()
    slow = TimedPanel(span, 0.05, name="slow", get_error=RuntimeError("down"))
    fast = TimedPanel(
        span,
        0.0,
        name="fast",
        clients=[make_panel_client("alice", [1], up=7, down=8)],
    )
    subscription = make_subscription(
        database,
        panels=[cast(XUiSession, slow), cast(XUiSession, fast)],
    )
    create_alice(database)

    from custom_types import BandwidthInfo

    assert subscription.bandwidth_svc.all_traffic() == {
        "alice": BandwidthInfo(7, 8, 15),
    }


class HoldPanel(FakePanel):
    """Holds the first request of a call until every panel has entered it."""

    def __init__(self, gate: threading.Barrier, **kwargs: Unpack[FakePanelOptions]) -> None:
        super().__init__(**kwargs)
        self._gate = gate
        self._held = False

    def _hold_once(self) -> None:
        if self._held:
            return
        self._held = True
        self._gate.wait(timeout=2)

    def get(self, url: str) -> Response:
        self._hold_once()
        return super().get(url)

    def post(self, url: str, **kwargs: object) -> Response:
        self._hold_once()
        return super().post(url, **kwargs)


def test_user_mutations_overlap(database: Database) -> None:
    # Each call does several requests per panel. A per-request sleep can
    # finish one panel's first request before the other panel starts, so
    # overlap is the first request of the call, not each HTTP round trip.
    def panels() -> list[object]:
        gate = threading.Barrier(2)
        return [
            HoldPanel(
                gate,
                name="one",
                inbounds=[make_inbound(1), make_inbound(2)],
                clients=[make_panel_client("alice", [1])],
            ),
            HoldPanel(
                gate,
                name="two",
                inbounds=[make_inbound(3), make_inbound(4)],
                clients=[make_panel_client("alice", [3])],
            ),
        ]

    create_alice(database)
    make_subscription(database, panels=panels()).business_svc.add_users("alice")
    make_subscription(database, panels=panels()).business_svc.update_user(
        "alice",
        enable=False,
    )
    make_subscription(database, panels=panels()).business_svc.update_uuid(
        "alice",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    make_subscription(database, panels=panels()).business_svc.delete_user("alice")


def test_update_uuid_marks_first_panel_when_later_fails_first(database: Database) -> None:
    span = _Span()
    slow = TimedPanel(
        span,
        0.05,
        name="slow",
        inbounds=[make_inbound(1)],
        clients=[make_panel_client("alice", [1])],
        post_payload={"success": False, "msg": "slow rejected", "obj": None},
    )
    fast = TimedPanel(
        span,
        0.0,
        name="fast",
        inbounds=[make_inbound(2)],
        clients=[make_panel_client("alice", [2])],
        post_payload={"success": False, "msg": "fast rejected", "obj": None},
    )
    subscription = make_subscription(
        database,
        panels=[cast(XUiSession, slow), cast(XUiSession, fast)],
    )
    create_alice(database)

    with pytest.raises(PanelRejectedError, match="slow rejected"):
        subscription.business_svc.update_uuid(
            "alice",
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )

    failures = subscription.business_code_svc.get_rollback_failures()
    assert failures["uuid"]["alice"]["reason"].startswith("slow:")
