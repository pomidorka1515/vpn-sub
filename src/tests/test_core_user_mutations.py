from __future__ import annotations

from typing import cast

import pytest

from core import Subscription
from db import Database
from errors import PanelRejectedError, ValidationError
from helpers import (
    USER_UUID,
    FakePanel,
    create_alice,
    make_client,
    make_inbound,
    make_subscription,
)
from session import XUiSession


def _alice_panel(**kwargs: object) -> FakePanel:
    inbound = make_inbound(1, [make_client(USER_UUID, up=1, down=2)])
    return FakePanel(inbounds=[inbound], **kwargs)  # type: ignore[arg-type]


def test_update_user_disables_regular_panel_only(
    database: Database,
) -> None:
    regular = _alice_panel(name="regular")
    whitelist = _alice_panel(name="whitelist", local=False)
    subscription = make_subscription(
        database, panels=[regular], whitelist_panel=whitelist,
    )
    create_alice(database)

    subscription.business_svc.update_user("alice", enable=False, timee=True)

    record = database.get_user("alice")
    assert record is not None
    assert (record["enabled"], record["enabled_time"]) == (0, 1)
    assert all("updateClient" in url for url, _ in regular.posts)
    assert whitelist.posts == []


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


def test_rejected_uuid_update_rolls_back_successful_inbounds(
    database: Database,
) -> None:
    panel = FakePanel(
        name="panel",
        inbounds=[
            make_inbound(1, [make_client(USER_UUID, up=1, down=1, inbound_id=1)]),
            make_inbound(2, [make_client(USER_UUID, up=2, down=2, inbound_id=2)]),
        ],
        post_queue=[
            {"success": True, "obj": None},
            {"success": False, "msg": "rejected", "obj": None, "status_code": 201},
            {"success": True, "obj": None},
        ],
    )
    subscription = make_subscription(database, panels=[panel])
    create_alice(database)

    with pytest.raises(PanelRejectedError):
        subscription.business_svc.update_uuid(
            "alice", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )

    record = database.get_user("alice")
    assert record is not None
    assert str(record["uuid"]) == USER_UUID
    assert len(panel.posts) == 3
    assert panel.posts[0][0].endswith(f"updateClient/{USER_UUID}")
    assert panel.posts[2][0].endswith(
        "updateClient/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )
    failures = subscription.business_code_svc.get_rollback_failures()
    assert failures["uuid"] == {}


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
