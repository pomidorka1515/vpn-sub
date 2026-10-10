from __future__ import annotations

import pytest
from helpers import make_subscription

from db import Database
from errors import ConflictError, DuplicateError, NotFoundError, ValidationError


def test_get_info_link_uses_configured_uri(database: Database) -> None:
    subscription = make_subscription(database, uri="custom")
    created = subscription.business_svc.add_new_user(
        "alice", "Alice", ext_username="alice-login",
        ext_password="secret", limit=10, wl_limit=5,
    )
    info = subscription.business_svc.get_info("alice")
    assert info.link == f"https://example.test/custom?token={created.token}"


def test_user_workflow_without_live_panels(database: Database) -> None:
    subscription = make_subscription(
        database, fingerprints=["chrome", "firefox"],
    )
    with pytest.raises(NotFoundError):
        subscription.business_svc.get_info("missing")
    created = subscription.business_svc.add_new_user(
        "alice", "Alice", ext_username="alice-login",
        ext_password="secret", limit=10, wl_limit=5,
    )
    with pytest.raises(ValidationError):
        subscription.business_svc.update_params("alice", fingerprint="invalid")
    with pytest.raises(ConflictError) as user_conflict:
        subscription.business_svc.add_new_user(
            "alice", "Alice", ext_username="alice-login",
            ext_password="secret", limit=10, wl_limit=5,
        )
    assert isinstance(user_conflict.value.__cause__, DuplicateError)
    with pytest.raises(ValidationError):
        subscription.code_svc.add_code("", "bonus")
    subscription.code_svc.add_code("invite", "bonus")
    with pytest.raises(ConflictError) as code_conflict:
        subscription.code_svc.add_code("invite", "bonus")
    assert isinstance(code_conflict.value.__cause__, DuplicateError)
    with pytest.raises(NotFoundError):
        subscription.code_svc.get_code("missing")
    with pytest.raises(NotFoundError):
        subscription.code_svc.delete_code("missing")
    assert created.username == "alice"
    assert subscription.password_svc.validate_credentials("alice-login", "secret") == "alice"
    subscription.telegram_svc.set_telegram_user(123, "alice")
    assert subscription.telegram_svc.get_username_telegram(123) == "alice"
    subscription.business_svc.update_params("alice", displayname="Alice 2", fingerprint="firefox")
    info = subscription.business_svc.get_info("alice")
    assert (info.displayname, info.fingerprint) == ("Alice 2", "firefox")
    assert info.link == f"https://example.test/sub?token={created.token}"
    subscription.business_svc.delete_user("alice", perma=True)
    assert not subscription.user_svc.isuser("alice")


def test_external_username_conflict_preserves_cause(database: Database) -> None:
    subscription = make_subscription(database)
    subscription.business_svc.add_new_user(
        "alice", "Alice", ext_username="alice-login", ext_password="secret",
    )
    subscription.business_svc.add_new_user("bob", "Bob")

    with pytest.raises(ConflictError) as conflict:
        subscription.business_svc.update_params("bob", ext_username="alice-login")

    assert isinstance(conflict.value.__cause__, DuplicateError)
