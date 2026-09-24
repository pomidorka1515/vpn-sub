from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

from core import Subscription
from db import Database
from errors import PanelRejectedError, ValidationError
from helpers import (
    USER_UUID,
    FakePanel,
    create_alice,
    make_inbound,
    make_panel_client,
    make_subscription,
)
from session import XUiSession

NEW_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _post_bodies(panel: FakePanel, fragment: str) -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], kwargs.get("json"))
        for url, kwargs in panel.posts
        if fragment in url
    ]


def test_add_users_creates_client_once_per_panel(database: Database) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1), make_inbound(2), make_inbound(3, protocol="trojan")],
    )
    subscription = make_subscription(database, panels=[cast(XUiSession, panel)])
    create_alice(database)

    subscription.business_svc.add_users("alice")

    adds = _post_bodies(panel, "clients/add")
    assert len(adds) == 1
    client = adds[0]["client"]
    assert client["email"] == "alice"
    assert client["id"] == USER_UUID
    assert client["flow"] == "xtls-rprx-vision"
    assert (client["limitIp"], client["totalGB"], client["expiryTime"]) == (0, 0, 0)
    assert client["enable"] is True
    assert adds[0]["inboundIds"] == [1, 2]  # trojan inbound excluded
    assert panel.clients[0].inboundIds == [1, 2]


def test_add_users_attaches_missing_inbounds(database: Database) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1), make_inbound(2)],
        clients=[make_panel_client("alice", [1])],
    )
    subscription = make_subscription(database, panels=[cast(XUiSession, panel)])
    create_alice(database)

    subscription.business_svc.add_users("alice")

    assert _post_bodies(panel, "clients/add") == []
    attaches = _post_bodies(panel, "/attach")
    assert attaches == [{"inboundIds": [2]}]
    assert panel.clients[0].inboundIds == [1, 2]


def test_add_users_is_noop_when_fully_attached(database: Database) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1)],
        clients=[make_panel_client("alice", [1])],
    )
    subscription = make_subscription(database, panels=[cast(XUiSession, panel)])
    create_alice(database)

    subscription.business_svc.add_users("alice")

    assert panel.posts == []


def test_add_users_skips_panel_without_vless_inbounds(database: Database) -> None:
    panel = FakePanel(name="panel", inbounds=[make_inbound(1, protocol="trojan")])
    subscription = make_subscription(database, panels=[cast(XUiSession, panel)])
    create_alice(database)

    subscription.business_svc.add_users("alice")

    assert panel.posts == []


def test_add_users_rejects_foreign_client_with_same_email(database: Database) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1)],
        clients=[make_panel_client("alice", [1], uuid="ffffffff-ffff-ffff-ffff-ffffffffffff")],
    )
    subscription = make_subscription(database, panels=[cast(XUiSession, panel)])
    create_alice(database)

    with pytest.raises(PanelRejectedError, match="different client"):
        subscription.business_svc.add_users("alice")


def test_delete_user_calls_clients_del_once_per_panel(database: Database) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1), make_inbound(2)],
        clients=[make_panel_client("alice", [1, 2])],
    )
    subscription = make_subscription(database, panels=[cast(XUiSession, panel)])
    create_alice(database)

    subscription.business_svc.delete_user("alice")

    assert [url for url, _ in panel.posts] == ["panel/api/clients/del/alice"]
    assert panel.clients == []
    assert database.user_exists("alice")


def test_update_user_disables_regular_panel_only(
    database: Database,
) -> None:
    regular = FakePanel(
        name="regular",
        inbounds=[make_inbound(1)],
        clients=[make_panel_client("alice", [1])],
    )
    whitelist = FakePanel(
        name="whitelist",
        inbounds=[make_inbound(2)],
        clients=[make_panel_client("alice", [2])],
        local=False,
    )
    subscription = make_subscription(
        database, panels=[regular], whitelist_panel=whitelist,
    )
    create_alice(database)

    subscription.business_svc.update_user("alice", enable=False, timee=True)

    record = database.get_user("alice")
    assert record is not None
    assert (record["enabled"], record["enabled_time"]) == (0, 1)
    assert [url for url, _ in regular.posts] == ["panel/api/clients/bulkDisable"]
    assert _post_bodies(regular, "bulkDisable") == [{"emails": ["alice"]}]
    assert regular.clients[0].enable is False
    assert whitelist.posts == []
    assert whitelist.clients[0].enable is True


def test_update_user_enable_uses_bulk_enable(database: Database) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1)],
        clients=[make_panel_client("alice", [1])],
    )
    subscription = make_subscription(database, panels=[cast(XUiSession, panel)])
    create_alice(database)

    subscription.business_svc.update_user("alice", enable=True)

    assert [url for url, _ in panel.posts] == ["panel/api/clients/bulkEnable"]
    assert panel.clients[0].enable is True


def test_update_user_rejection_carries_panel_msg(database: Database) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1)],
        post_payload={"success": False, "msg": "client not found: alice", "obj": None},
    )
    subscription = make_subscription(database, panels=[cast(XUiSession, panel)])
    create_alice(database)

    with pytest.raises(PanelRejectedError, match="client not found"):
        subscription.business_svc.update_user("alice", enable=False)


def test_whitelist_status_persists_without_whitelist_panel(
    database: Database, subscription: Subscription,
) -> None:
    create_alice(database)
    subscription.business_svc.update_user("alice", wl_enable=False)
    record = database.get_user("alice")
    assert record is not None
    assert record["enabled_wl"] == 0


def test_update_uuid_rejects_invalid_value(
    database: Database, subscription: Subscription,
) -> None:
    create_alice(database)
    with pytest.raises(ValidationError):
        subscription.business_svc.update_uuid("alice", "not-a-uuid")
    record = database.get_user("alice")
    assert record is not None
    assert str(record["uuid"]) == USER_UUID


def test_update_uuid_sends_full_row_with_new_id(database: Database) -> None:
    existing = replace(
        make_panel_client("alice", [1, 2]),
        subId="sub-123", comment="keep me", tgId="777",
    )
    panel = FakePanel(name="panel", inbounds=[make_inbound(1)], clients=[existing])
    subscription = make_subscription(database, panels=[cast(XUiSession, panel)])
    create_alice(database)

    subscription.business_svc.update_uuid("alice", NEW_UUID)

    updates = _post_bodies(panel, "clients/update/")
    assert len(updates) == 1
    assert updates[0]["id"] == NEW_UUID
    assert updates[0]["email"] == "alice"
    assert updates[0]["subId"] == "sub-123"
    assert updates[0]["comment"] == "keep me"
    assert updates[0]["tgId"] == "777"
    assert panel.clients[0].uuid == NEW_UUID
    record = database.get_user("alice")
    assert record is not None
    assert str(record["uuid"]) == NEW_UUID


def test_update_uuid_skips_panels_without_client(database: Database) -> None:
    synced = FakePanel(
        name="synced",
        inbounds=[make_inbound(1)],
        clients=[make_panel_client("alice", [1])],
    )
    unsynced = FakePanel(name="unsynced", inbounds=[make_inbound(2)])
    subscription = make_subscription(
        database, panels=[cast(XUiSession, synced), cast(XUiSession, unsynced)],
    )
    create_alice(database)

    subscription.business_svc.update_uuid("alice", NEW_UUID)

    assert [url for url, _ in synced.posts] == [f"panel/api/clients/update/alice"]
    assert unsynced.posts == []


def test_rejected_uuid_update_marks_failure_without_compensation(
    database: Database,
) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1), make_inbound(2)],
        clients=[make_panel_client("alice", [1, 2])],
        post_queue=[
            {"success": False, "msg": "rejected", "obj": None, "status_code": 201},
        ],
    )
    subscription = make_subscription(database, panels=[cast(XUiSession, panel)])
    create_alice(database)

    with pytest.raises(PanelRejectedError):
        subscription.business_svc.update_uuid("alice", NEW_UUID)

    record = database.get_user("alice")
    assert record is not None
    assert str(record["uuid"]) == USER_UUID
    # one atomic attempt per panel, no multi-inbound compensation loop
    assert len(panel.posts) == 1
    failures = subscription.business_code_svc.get_rollback_failures()
    assert "panel" in failures["uuid"]["alice"]["reason"]


def test_uuid_db_conflict_marks_failure(database: Database) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1)],
        clients=[make_panel_client("alice", [1])],
    )
    subscription = make_subscription(database, panels=[cast(XUiSession, panel)])
    create_alice(database)
    database.create_user(
        username="bob", uuid=NEW_UUID, token="b" * 40,
        fingerprint="chrome", displayname="Bob",
    )

    from errors import ConflictError
    with pytest.raises(ConflictError):
        subscription.business_svc.update_uuid("alice", NEW_UUID)

    failures = subscription.business_code_svc.get_rollback_failures()
    assert failures["uuid"]["alice"]["reason"] == "db duplicate uuid"


def test_add_new_user_rolls_back_db_row_when_panel_rejects(
    database: Database,
) -> None:
    panel = FakePanel(
        inbounds=[make_inbound(1)],
        post_payload={"success": False, "msg": "rejected", "obj": None},
        post_status=201,
    )
    subscription = make_subscription(database, panels=[cast(XUiSession, panel)])
    with pytest.raises(PanelRejectedError):
        subscription.business_svc.add_new_user(
            "alice", "Alice", ext_username="alice-login",
            ext_password="secret", limit=10, wl_limit=5,
        )
    assert not database.user_exists("alice")


def test_add_users_quotes_username_in_attach_path(database: Database) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1), make_inbound(2)],
        clients=[make_panel_client("quoted_user", [1])],
    )
    subscription = make_subscription(database, panels=[cast(XUiSession, panel)])
    database.create_user(
        username="quoted_user", uuid=USER_UUID, token="q" * 40,
        fingerprint="chrome", displayname="Quoted",
    )

    subscription.business_svc.add_users("quoted_user")

    assert panel.posts[0][0] == "panel/api/clients/quoted_user/attach"


def test_delete_user_quotes_username_in_path(database: Database) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[make_inbound(1)],
        clients=[make_panel_client("quoted_user", [1])],
    )
    subscription = make_subscription(database, panels=[cast(XUiSession, panel)])
    database.create_user(
        username="quoted_user", uuid=USER_UUID, token="q" * 40,
        fingerprint="chrome", displayname="Quoted",
    )

    subscription.business_svc.delete_user("quoted_user")

    assert panel.posts[0][0] == "panel/api/clients/del/quoted_user"


def test_add_new_user_rejects_panel_unsafe_username(database: Database) -> None:
    subscription = make_subscription(database)
    with pytest.raises(ValidationError):
        subscription.business_svc.add_new_user("bad name", "Bad")
    with pytest.raises(ValidationError):
        subscription.business_svc.add_new_user("sl/ash", "Slash")
    assert not database.user_exists("bad name")
