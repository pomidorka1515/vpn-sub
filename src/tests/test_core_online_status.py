from __future__ import annotations

from typing import cast
import sqlite3

import pytest

from core import Subscription
from errors import PanelUnavailableError
from session import XUiSession
from helpers import FakePanel
from db import Database
from helpers import create_alice


def test_online_status_reports_empty_when_all_panels_succeed(
    subscription: Subscription,
) -> None:
    subscription.panels.append(cast(XUiSession, FakePanel(name="panel")))
    status = subscription.panel_svc.get_online_status()
    assert status.users == []
    assert status.panel_health == {"panel": "ok"}


def test_online_status_raises_when_all_panels_fail(subscription: Subscription) -> None:
    subscription.res.panels.append(
        cast(XUiSession, FakePanel(name="panel", post_error=RuntimeError("transport failed")))
    )
    with pytest.raises(PanelUnavailableError):
        subscription.panel_svc.get_online_status()


def test_online_status_reports_empty_without_panels(subscription: Subscription) -> None:
    status = subscription.panel_svc.get_online_status()
    assert status.users == []
    assert status.panel_health == {}
    assert subscription.panel_svc.get_online_status(new=True).users == {}


def test_online_status_marks_malformed_payload_invalid(subscription: Subscription) -> None:
    subscription.panels.append(
        cast(XUiSession, FakePanel(name="panel", post_payload={"success": True, "obj": 7}))
    )
    status = subscription.panel_svc.get_online_status()
    assert status.panel_health == {"panel": "invalid"}


def test_online_status_loads_users_once(
    database: Database, subscription: Subscription,
) -> None:
    create_alice(database, ext_username="ext-alice")
    database.create_user(
        username="bob", uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        token="b" * 40, fingerprint="chrome", displayname="Bob",
    )
    payload = {"success": True, "obj": ["alice", "stranger", "bob", "alice"]}
    subscription.panels.append(cast(XUiSession, FakePanel(name="a", post_payload=payload)))
    subscription.panels.append(cast(XUiSession, FakePanel(name="b", post_payload=payload)))
    seen: list[str] = []
    original = database._connect

    def traced() -> sqlite3.Connection:
        conn = original()
        conn.set_trace_callback(seen.append)
        return conn

    database._connect = traced  # type: ignore[method-assign]
    try:
        status = subscription.panel_svc.get_online_status(new=True)
    finally:
        database._connect = original  # type: ignore[method-assign]

    assert status.users == {"alice": "ext-alice", "bob": None}
    selects = [q for q in seen if q.startswith("SELECT")]
    assert len(selects) == 2
