from __future__ import annotations

from typing import cast

from db import Database
from errors import PanelRejectedError
from helpers import (
    FakePanel,
    create_alice,
    make_inbound,
    make_panel_client,
    make_subscription,
    make_watch,
)
from session import XUiSession


class _RecordingAdminBot:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def msg(self, text: str, parse_mode: str = "HTML") -> None:
        self.messages.append(text)


def test_reconcile_attaches_new_inbound_to_existing_client(database: Database) -> None:
    # admin added inbound 2 after the user was created
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1), make_inbound(2)],
        clients=[make_panel_client("alice", [1])],
    )
    subscription = make_subscription(database, panels=[cast(XUiSession, panel)])
    create_alice(database)
    watch = make_watch(database, subscription)

    watch.reconcile_inbounds()

    attach_bodies = [
        post["json"] for url, post in panel.posts
        if url == "panel/api/clients/alice/attach"
    ]
    assert attach_bodies == [{"inboundIds": [2]}]
    # no create, no delete: the client already exists with the right uuid
    urls = [url for url, _ in panel.posts]
    assert "panel/api/clients/add" not in urls


def test_reconcile_creates_missing_client(database: Database) -> None:
    panel = FakePanel(name="panel", inbounds=[make_inbound(1)], clients=[])
    subscription = make_subscription(database, panels=[cast(XUiSession, panel)])
    create_alice(database)
    watch = make_watch(database, subscription)

    watch.reconcile_inbounds()

    urls = [url for url, _ in panel.posts]
    assert "panel/api/clients/add" in urls


def test_reconcile_is_idempotent_noop(database: Database) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1)],
        clients=[make_panel_client("alice", [1])],
    )
    subscription = make_subscription(database, panels=[cast(XUiSession, panel)])
    create_alice(database)
    watch = make_watch(database, subscription)

    watch.reconcile_inbounds()

    assert panel.posts == []


def test_reconcile_continues_after_user_failure_and_alerts(
    database: Database,
) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1)],
        clients=[make_panel_client("alice", [1])],
    )
    subscription = make_subscription(database, panels=[cast(XUiSession, panel)])
    create_alice(database)
    database.create_user(
        username="bob", uuid="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        token="b" * 40, fingerprint="chrome", displayname="Bob",
    )
    admin_bot = _RecordingAdminBot()
    watch = make_watch(database, subscription, admin_bot=admin_bot)

    calls: list[str] = []

    def add_users(username: str, _called_internally: bool = False) -> None:
        calls.append(username)
        if username == "alice":
            raise PanelRejectedError("foreign client owns the email")

    subscription.business_svc.add_users = add_users  # type: ignore[method-assign]
    watch.reconcile_inbounds()

    # bob still reconciled after alice failed
    assert calls == ["alice", "bob"]
    assert len(admin_bot.messages) == 1
    assert "alice" in admin_bot.messages[0]
