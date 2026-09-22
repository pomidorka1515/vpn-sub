from __future__ import annotations

from helpers import TOKEN_A, create_alice
from db import Database
from core import Subscription


def test_reset_user_invalidates_auth_session(
    database: Database, subscription: Subscription,
) -> None:
    create_alice(database)
    database.set_auth_token("alice", "b" * 100)

    result = subscription.business_svc.reset_user("alice")

    assert result.token != TOKEN_A
    assert subscription.user_svc.get_token("alice") == result.token
    record = database.get_user("alice")
    assert record is not None
    assert record["auth_token"] is None
    assert subscription.user_svc.auth_token_to_user("b" * 100) is None
