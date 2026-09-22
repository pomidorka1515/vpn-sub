from __future__ import annotations

import uuid

import pytest

from core import Subscription
from db import Database
from helpers import make_subscription


def test_startup_recovers_registration_rollback_marker(
    database: Database, subscription: Subscription,
) -> None:
    database.add_code("invite", "register", uses=1)
    database.register_with_code(
        code="invite", username="alice", uuid=str(uuid.uuid4()),
        token="a" * 40, fingerprint="chrome", displayname="Alice",
        ext_username="alice-login", ext_password_hash="hash",
    )
    database.set_metadata("registration_rollback_failed:alice", "123")
    subscription.business_code_svc.recover_rollback_failures()
    assert database.get_metadata("registration_rollback_failed:alice") is None
    assert not database.user_exists("alice")


def test_rollback_marker_reporting_and_resolution(
    database: Database, subscription: Subscription,
) -> None:
    database.set_metadata("uuid_rollback_failed:alice", "123:panel rejected")
    database.set_metadata("registration_rollback_failed:bob", "124")
    failures = subscription.business_code_svc.get_rollback_failures()
    assert failures["uuid"]["alice"]["reason"] == "panel rejected"
    assert failures["registration"]["bob"]["ts"] == "124"
    subscription.business_code_svc.clear_rollback_failure("uuid", "alice")
    assert subscription.business_code_svc.get_rollback_failures()["uuid"] == {}


def test_registration_rollback_failure_preserves_sync_error(database: Database) -> None:
    subscription = make_subscription(database)
    database.add_code("invite", "register", days=1, gb=1, wl_gb=0, permanent=False, uses=1)
    sync_error = RuntimeError("panel transport failed")

    def fail_sync(username: str, _called_internally: bool = False) -> None:
        raise sync_error

    def fail_rollback(username: str) -> None:
        raise RuntimeError("database rollback failed")

    subscription.business_svc.add_users = fail_sync  # type: ignore[method-assign]
    database.rollback_registration_sync = fail_rollback  # type: ignore[method-assign]
    with pytest.raises(RuntimeError) as raised:
        subscription.business_code_svc.register_with_code(
            code="invite", username="alice", displayname="Alice",
            ext_username="alice-login", ext_password="secret",
        )
    assert raised.value is sync_error
    assert database.get_metadata("registration_rollback_failed:alice") is not None
