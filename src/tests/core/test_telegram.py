from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from errors import NotFoundError
from tests.support.users import create_alice

if TYPE_CHECKING:
    from core import Subscription
    from db import Database


@pytest.mark.asyncio
async def test_telegram_language_round_trip(subscription: Subscription) -> None:
    assert (await subscription.telegram_svc.get_telegram_language(123)) == "ru"
    assert not (await subscription.telegram_svc.has_telegram_language(123))
    (await subscription.telegram_svc.set_telegram_language(123, "en"))
    assert await subscription.telegram_svc.has_telegram_language(123)
    assert (await subscription.telegram_svc.get_telegram_language(123)) == "en"


@pytest.mark.asyncio
async def test_telegram_bind_unbind_and_reverse_lookup(
    database: Database,
    subscription: Subscription,
) -> None:
    (await create_alice(database))
    (await subscription.telegram_svc.set_telegram_user(123, "alice"))
    assert await subscription.telegram_svc.is_registered(123)
    assert (await subscription.telegram_svc.get_username_telegram(123)) == "alice"
    assert (await subscription.telegram_svc.get_username_telegram("alice", reverse=True)) == 123
    info = await subscription.telegram_svc.get_info_telegram(123)
    assert info.displayname == "Alice"
    (await subscription.telegram_svc.set_telegram_user(123, None))
    assert not (await subscription.telegram_svc.is_registered(123))
    assert (await subscription.telegram_svc.get_username_telegram(123)) is None
    assert (await subscription.telegram_svc.get_username_telegram("alice", reverse=True)) is None


@pytest.mark.asyncio
async def test_unlink_missing_telegram_mapping_is_noop(
    database: Database,
    subscription: Subscription,
) -> None:
    await create_alice(database)
    await subscription.telegram_svc.set_telegram_user(456, "alice")

    await subscription.telegram_svc.set_telegram_user(123, None)
    assert await subscription.telegram_svc.get_username_telegram(456) == "alice"


@pytest.mark.asyncio
async def test_unlink_preserves_concurrent_telegram_relink(
    database: Database,
    subscription: Subscription,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await create_alice(database)
    await subscription.telegram_svc.set_telegram_user(123, "alice")
    looked_up, release = asyncio.Event(), asyncio.Event()
    original = database.tgid_to_user

    async def pause_after_lookup(tgid: str | int) -> str | None:
        username = await original(tgid)
        if str(tgid) == "123":
            looked_up.set()
            await release.wait()
        return username

    monkeypatch.setattr(database, "tgid_to_user", pause_after_lookup)
    unlink = asyncio.create_task(subscription.telegram_svc.set_telegram_user(123, None))
    try:
        await asyncio.wait_for(looked_up.wait(), timeout=5)
        await asyncio.wait_for(subscription.telegram_svc.set_telegram_user(456, "alice"), timeout=5)
        release.set()
        await asyncio.wait_for(unlink, timeout=5)
        assert await original(123) is None
        assert await original(456) == "alice"
        assert await database.user_to_tgid("alice") == "456"
    finally:
        release.set()
        await asyncio.gather(unlink, return_exceptions=True)


@pytest.mark.asyncio
async def test_telegram_bonus_requires_bound_user(
    database: Database,
    subscription: Subscription,
) -> None:
    (await create_alice(database, bw_limit_gb=1, wl_limit_gb=1, expires_at=1000))
    (await subscription.code_svc.add_code("bonus1", "bonus", gb=2, wl_gb=3, uses=1))
    with pytest.raises(NotFoundError):
        (await subscription.telegram_svc.bonus_code(123, "bonus1"))
    (await subscription.telegram_svc.set_telegram_user(123, "alice"))
    result = await subscription.telegram_svc.bonus_code(123, "bonus1")
    assert result.gb == 2
    assert result.wl_gb == 3


@pytest.mark.asyncio
async def test_get_info_telegram_unknown_id(subscription: Subscription) -> None:
    with pytest.raises(NotFoundError):
        (await subscription.telegram_svc.get_info_telegram(999))
