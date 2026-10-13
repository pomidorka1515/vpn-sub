from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from helpers import FakePanel, create_alice, make_inbound, make_panel_client, panel_client

from errors import PanelRejectedError

if TYPE_CHECKING:
    from core import Subscription
    from db import Database


@pytest.mark.asyncio
async def test_add_users_raises_panel_rejected_error(
    database: Database,
    subscription: Subscription,
) -> None:
    (await create_alice(database, ext_username="alice-login", ext_password_hash="hash"))
    panel = FakePanel(
        inbounds=[make_inbound(1)],
        local=False,
        post_payload={"success": False, "msg": "rejected", "obj": None},
        post_status=201,
    )
    await panel_client(subscription, panel)
    with pytest.raises(PanelRejectedError):
        (await subscription.business_svc.add_users("alice"))


@pytest.mark.asyncio
async def test_delete_user_raises_panel_rejected_error(
    database: Database,
    subscription: Subscription,
) -> None:
    (await create_alice(database))
    panel = FakePanel(
        inbounds=[make_inbound(1)],
        # the client must exist on the panel for the delete to be attempted
        clients=[make_panel_client("alice", [1])],
        post_payload={"success": False, "msg": "rejected", "obj": None},
    )
    await panel_client(subscription, panel)
    with pytest.raises(PanelRejectedError):
        (await subscription.business_svc.delete_user("alice"))
    # perma DB delete must not run when the panel rejected
    assert (await database.user_exists("alice"))


@pytest.mark.asyncio
async def test_update_uuid_raises_panel_rejected_error(
    database: Database,
    subscription: Subscription,
) -> None:
    (await create_alice(database))
    panel = FakePanel(
        inbounds=[make_inbound(1)],
        post_payload={"success": False, "msg": "rejected", "obj": None},
        post_status=201,
    )
    await panel_client(subscription, panel)
    from helpers import make_panel_client

    panel.clients.append(make_panel_client("alice", [1]))
    with pytest.raises(PanelRejectedError):
        (await subscription.business_svc.update_uuid(
            "alice",
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        ))
