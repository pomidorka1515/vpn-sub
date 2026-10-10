from __future__ import annotations

import sqlite3
import threading
from typing import cast

import pytest
from helpers import FakePanel, create_alice
from requests import Response

from config import JsonValue
from core import Subscription
from core.services.panel import _ONLINES_TTL
from db import Database
from errors import PanelUnavailableError
from session import XUiSession


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
    payload: dict[str, JsonValue] = {"success": True, "obj": ["alice", "stranger", "bob", "alice"]}
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


def test_online_status_reuses_a_fresh_classification(
    database: Database, subscription: Subscription,
) -> None:
    create_alice(database)
    panel = FakePanel(name="panel", post_payload={"success": True, "obj": ["alice"]})
    subscription.panels.append(cast(XUiSession, panel))

    first = subscription.panel_svc.get_online_status()
    second = subscription.panel_svc.get_online_status(new=True)
    assert first.users == ["alice"]
    assert second.users == {"alice": None}
    assert subscription.panel_svc.is_online("alice")
    assert len(panel.posts) == 1

    cached = subscription.panel_svc._onlines
    assert cached is not None
    cached.stored_at -= _ONLINES_TTL
    assert subscription.panel_svc.get_online_status().users == ["alice"]
    assert len(panel.posts) == 2


def test_online_status_does_not_cache_a_total_outage(
    subscription: Subscription,
) -> None:
    panel = FakePanel(name="panel", post_error=RuntimeError("transport failed"))
    subscription.panels.append(cast(XUiSession, panel))

    with pytest.raises(PanelUnavailableError):
        subscription.panel_svc.get_online_status()
    with pytest.raises(PanelUnavailableError):
        subscription.panel_svc.get_online_status()
    assert len(panel.posts) == 2


def test_online_status_shares_one_inflight_fetch(
    database: Database, subscription: Subscription,
) -> None:
    create_alice(database)
    started = threading.Event()
    release = threading.Event()

    class GatedPanel(FakePanel):
        def post(self, url: str, **kwargs: object) -> Response:
            started.set()
            assert release.wait(1)
            return super().post(url, **kwargs)

    panel = GatedPanel(name="panel", post_payload={"success": True, "obj": ["alice"]})
    subscription.panels.append(cast(XUiSession, panel))
    errors: list[BaseException] = []
    results: list[list[str] | dict[str, str | None]] = []

    def read() -> None:
        try:
            results.append(subscription.panel_svc.get_online_users())
        except BaseException as exc:
            errors.append(exc)

    follower = threading.Thread(target=read)
    leader = threading.Thread(target=read)
    leader.start()
    assert started.wait(1)
    follower.start()
    # The follower must be waiting on the leader, not on its own post.
    follower.join(0.05)
    assert follower.is_alive()
    release.set()
    leader.join(1)
    follower.join(1)

    assert errors == []
    assert results == [["alice"], ["alice"]]
    assert len(panel.posts) == 1
