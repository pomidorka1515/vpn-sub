from __future__ import annotations

import pytest

from core import Subscription
from db import Database
from errors import NotFoundError, ValidationError
from helpers import create_alice


def test_add_code_rejects_unknown_action(subscription: Subscription) -> None:
    with pytest.raises(ValidationError):
        subscription.code_svc.add_code("invite", "gift")


def test_list_and_get_code_round_trip(subscription: Subscription) -> None:
    subscription.code_svc.add_code(
        "bonus1", "bonus", days=2, gb=3, wl_gb=4, uses=2,
    )
    assert subscription.code_svc.list_code() == ["bonus1"]
    code = subscription.code_svc.get_code("bonus1")
    assert (code.action, code.days, code.gb, code.wl_gb, code.uses) == (
        "bonus", 2, 3, 4, 2,
    )


def test_apply_bonus_code_updates_user_limits(
    database: Database, subscription: Subscription,
) -> None:
    create_alice(database, bw_limit_gb=1, wl_limit_gb=2, expires_at=1000)
    subscription.code_svc.add_code(
        "bonus1", "bonus", days=1, gb=3, wl_gb=4, uses=1,
    )
    result = subscription.code_svc.apply_bonus_code(username="alice", code="bonus1")
    record = database.get_user("alice")
    assert record is not None
    assert result.gb == 3
    assert result.limit == 4
    assert result.wl_limit == 6
    assert record["bw_limit_gb"] == 4
    assert record["wl_limit_gb"] == 6
    with pytest.raises(NotFoundError):
        subscription.code_svc.get_code("bonus1")


def test_apply_bonus_code_rejects_unknown_user(
    subscription: Subscription,
) -> None:
    subscription.code_svc.add_code("bonus1", "bonus")
    with pytest.raises(NotFoundError):
        subscription.code_svc.apply_bonus_code(username="missing", code="bonus1")


def test_register_with_code_creates_user(
    database: Database, subscription: Subscription,
) -> None:
    subscription.code_svc.add_code(
        "invite", "register", days=1, gb=2, wl_gb=3, uses=1,
    )
    created = subscription.business_code_svc.register_with_code(
        code="invite", username="alice", displayname="Alice",
        ext_username="alice-login", ext_password="secret",
    )
    assert created.username == "alice"
    assert created.limit == 2
    assert created.wl_limit == 3
    assert database.user_exists("alice")
    assert subscription.password_svc.validate_credentials(
        "alice-login", "secret",
    ) == "alice"


def test_register_with_code_rejects_empty_code(
    subscription: Subscription,
) -> None:
    with pytest.raises(ValidationError):
        subscription.business_code_svc.register_with_code(
            code="", username="alice", displayname="Alice",
            ext_username="alice-login", ext_password="secret",
        )
