from __future__ import annotations

from typing import cast

import pytest

from core import Subscription
from db import Database
from errors import PanelRejectedError
from session import XUiSession
from helpers import FakePanel, create_alice, make_inbound


def test_add_users_raises_panel_rejected_error(
    database: Database, subscription: Subscription,
) -> None:
    create_alice(database, ext_username="alice-login", ext_password_hash="hash")
    panel = FakePanel(
        inbounds=[make_inbound(1)],
        local=False,
        post_payload={"success": False, "msg": "rejected", "obj": None},
        post_status=201,
    )
    subscription.res.panels.append(cast(XUiSession, panel))
    with pytest.raises(PanelRejectedError):
        subscription.business_svc.add_users("alice")
