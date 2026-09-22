from __future__ import annotations

import pytest

from core import Subscription
from db import Database
from errors import NotFoundError
from helpers import create_alice


def test_telegram_language_round_trip(subscription: Subscription) -> None:
    assert subscription.telegram_svc.get_telegram_language(123) == "ru"
    assert not subscription.telegram_svc.has_telegram_language(123)
    subscription.telegram_svc.set_telegram_language(123, "en")
    assert subscription.telegram_svc.has_telegram_language(123)
    assert subscription.telegram_svc.get_telegram_language(123) == "en"


def test_telegram_bind_unbind_and_reverse_lookup(
    database: Database, subscription: Subscription,
) -> None:
    create_alice(database)
    subscription.telegram_svc.set_telegram_user(123, "alice")
    assert subscription.telegram_svc.is_registered(123)
    assert subscription.telegram_svc.get_username_telegram(123) == "alice"
    assert subscription.telegram_svc.get_username_telegram("alice", reverse=True) == 123
    info = subscription.telegram_svc.get_info_telegram(123)
    assert info.displayname == "Alice"
    subscription.telegram_svc.set_telegram_user(123, None)
    assert not subscription.telegram_svc.is_registered(123)
    assert subscription.telegram_svc.get_username_telegram(123) is None


def test_telegram_bonus_requires_bound_user(
    database: Database, subscription: Subscription,
) -> None:
    create_alice(database, bw_limit_gb=1, wl_limit_gb=1, expires_at=1000)
    subscription.code_svc.add_code("bonus1", "bonus", gb=2, wl_gb=3, uses=1)
    with pytest.raises(NotFoundError):
        subscription.telegram_svc.bonus_code(123, "bonus1")
    subscription.telegram_svc.set_telegram_user(123, "alice")
    result = subscription.telegram_svc.bonus_code(123, "bonus1")
    assert result.gb == 2
    assert result.wl_gb == 3


def test_get_info_telegram_unknown_id(subscription: Subscription) -> None:
    with pytest.raises(NotFoundError):
        subscription.telegram_svc.get_info_telegram(999)
