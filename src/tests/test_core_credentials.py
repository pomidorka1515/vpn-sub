from __future__ import annotations

from dataclasses import replace as dreplace

from argon2 import PasswordHasher
import pytest

from core import Subscription
from db import Database
from helpers import create_alice


def _create_login(database: Database, password_hash: str) -> None:
    create_alice(
        database, ext_username="alice-login", ext_password_hash=password_hash,
    )


def test_validate_credentials_rejects_wrong_argon2_password(
    database: Database, subscription: Subscription,
) -> None:
    _create_login(database, PasswordHasher().hash("secret"))
    assert subscription.password_svc.validate_credentials("alice-login", "wrong") is None
    assert subscription.password_svc.validate_credentials("alice-login", "secret") == "alice"


def test_valid_legacy_hash_is_migrated_to_argon2(
    database: Database, subscription: Subscription,
) -> None:
    _create_login(database, subscription.password_svc.legacy_hash("secret"))
    assert subscription.password_svc.validate_credentials("alice-login", "secret") == "alice"
    stored = database.ext_password("alice-login")
    assert stored is not None
    assert stored.startswith("$argon2id$")

    subscription.res = dreplace(subscription.res, legacy_salt="changed-legacy-salt")
    assert subscription.password_svc.validate_credentials("alice-login", "secret") == "alice"


def test_validate_credentials_logs_corrupt_argon2_hash(
    database: Database, subscription: Subscription, caplog: pytest.LogCaptureFixture,
) -> None:
    _create_login(database, "$argon2id$corrupt")
    subscription.res.log.addHandler(caplog.handler)
    try:
        assert subscription.password_svc.validate_credentials("alice-login", "secret") is None
    finally:
        subscription.res.log.removeHandler(caplog.handler)
    assert "Corrupt or tampered argon2 password hash" in caplog.text
