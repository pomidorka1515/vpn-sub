from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from helpers import FakePanel, make_inbound, make_subscription

from errors import PanelRejectedError

if TYPE_CHECKING:
    from core import Subscription
    from db import Database


@pytest.mark.asyncio
async def test_startup_recovers_registration_rollback_marker(
    database: Database,
    subscription: Subscription,
) -> None:
    (await database.add_code("invite", "register", uses=1))
    (await database.register_with_code(
        code="invite",
        username="alice",
        uuid=str(uuid.uuid4()),
        token="a" * 40,
        fingerprint="chrome",
        displayname="Alice",
        ext_username="alice-login",
        ext_password_hash="hash",
    ))
    (await database.set_metadata("registration_rollback_failed:alice", "123"))
    (await subscription.business_code_svc.recover_rollback_failures())
    assert (await database.get_metadata("registration_rollback_failed:alice")) is None
    assert not (await database.user_exists("alice"))


@pytest.mark.asyncio
async def test_rollback_marker_reporting_and_resolution(
    database: Database,
    subscription: Subscription,
) -> None:
    (await database.set_metadata("uuid_rollback_failed:alice", "123:panel rejected"))
    (await database.set_metadata("registration_rollback_failed:bob", "124"))
    failures = (await subscription.business_code_svc.get_rollback_failures())
    assert failures["uuid"]["alice"]["reason"] == "panel rejected"
    assert failures["registration"]["bob"]["ts"] == "124"
    (await subscription.business_code_svc.clear_rollback_failure("uuid", "alice"))
    assert (await subscription.business_code_svc.get_rollback_failures())["uuid"] == {}


@pytest.mark.asyncio
async def test_registration_partial_panel_writes_are_removed_before_refund(database: Database) -> None:
    accepted = FakePanel(name="accepted", inbounds=[make_inbound(1)])
    rejected = FakePanel(
        name="rejected",
        inbounds=[make_inbound(1)],
        post_payload={"success": False, "msg": "rejected", "obj": None},
    )
    subscription = (await make_subscription(database, panels=[accepted, rejected]))
    (await database.add_code("invite", "register"))
    with pytest.raises(PanelRejectedError):
        (await subscription.business_code_svc.register_with_code(
            code="invite",
            username="alice",
            displayname="Alice",
            ext_username="alice",
            ext_password="secret",
        ))
    assert accepted.clients == []
    assert not (await database.user_exists("alice"))
    code = (await database.get_code("invite"))
    assert code is not None
    assert code["uses"] == 1


@pytest.mark.asyncio
async def test_admin_creation_partial_panel_writes_are_removed(database: Database) -> None:
    accepted = FakePanel(name="accepted", inbounds=[make_inbound(1)])
    rejected = FakePanel(
        name="rejected",
        inbounds=[make_inbound(1)],
        post_payload={"success": False, "msg": "rejected", "obj": None},
    )
    subscription = (await make_subscription(database, panels=[accepted, rejected]))
    with pytest.raises(PanelRejectedError):
        (await subscription.business_svc.add_new_user("alice", "Alice"))
    assert accepted.clients == []
    assert not (await database.user_exists("alice"))
