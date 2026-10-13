from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
from helpers import create_alice

from errors import NotFoundError, ValidationError

if TYPE_CHECKING:
    from core import Subscription
    from db import Database


@pytest.mark.asyncio
async def test_add_code_rejects_unknown_action(subscription: Subscription) -> None:
    with pytest.raises(ValidationError):
        (await subscription.code_svc.add_code("invite", "gift"))


@pytest.mark.asyncio
async def test_list_and_get_code_round_trip(subscription: Subscription) -> None:
    (await subscription.code_svc.add_code(
        "bonus1",
        "bonus",
        days=2,
        gb=3,
        wl_gb=4,
        uses=2,
    ))
    assert (await subscription.code_svc.list_code()) == ["bonus1"]
    code = (await subscription.code_svc.get_code("bonus1"))
    assert (code.action, code.days, code.gb, code.wl_gb, code.uses) == (
        "bonus",
        2,
        3,
        4,
        2,
    )


@pytest.mark.asyncio
async def test_apply_bonus_code_updates_user_limits(
    database: Database,
    subscription: Subscription,
) -> None:
    (await create_alice(database, bw_limit_gb=1, wl_limit_gb=2, expires_at=1000))
    (await subscription.code_svc.add_code(
        "bonus1",
        "bonus",
        days=1,
        gb=3,
        wl_gb=4,
        uses=1,
    ))
    result = (await subscription.code_svc.apply_bonus_code(username="alice", code="bonus1"))
    record = (await database.get_user("alice"))
    assert record is not None
    assert result.gb == 3
    assert result.limit == 4
    assert result.wl_limit == 6
    assert record["bw_limit_gb"] == 4
    assert record["wl_limit_gb"] == 6
    with pytest.raises(NotFoundError):
        (await subscription.code_svc.get_code("bonus1"))


@pytest.mark.asyncio
async def test_apply_bonus_code_grants_quota_on_zero_limit(
    database: Database,
    subscription: Subscription,
) -> None:
    (await create_alice(database, bw_limit_gb=0, wl_limit_gb=0, expires_at=0))
    (await database.update_user("alice", status=False, status_time=False, status_wl=False))
    (await subscription.code_svc.add_code(
        "bonus1",
        "bonus",
        days=0,
        gb=3,
        wl_gb=4,
        uses=1,
    ))
    result = (await subscription.code_svc.apply_bonus_code(username="alice", code="bonus1"))
    record = (await database.get_user("alice"))
    assert record is not None
    assert result.limit == 3
    assert result.wl_limit == 4
    assert record["bw_limit_gb"] == 3
    assert record["wl_limit_gb"] == 4
    assert record["expires_at"] == 0


@pytest.mark.asyncio
async def test_apply_bonus_code_restarts_lapsed_expiry(
    database: Database,
    subscription: Subscription,
) -> None:
    (await create_alice(database, bw_limit_gb=1, wl_limit_gb=1, expires_at=1))
    (await database.update_user("alice", status=False, status_time=False))
    (await subscription.code_svc.add_code(
        "bonus1",
        "bonus",
        days=2,
        gb=0,
        wl_gb=0,
        uses=1,
    ))
    before = int(time.time())
    result = (await subscription.code_svc.apply_bonus_code(username="alice", code="bonus1"))
    after = int(time.time())
    record = (await database.get_user("alice"))
    assert record is not None
    assert before + 2 * 86400 <= result.time <= after + 2 * 86400
    assert record["expires_at"] == result.time
    assert record["bw_limit_gb"] == 1


@pytest.mark.asyncio
async def test_apply_bonus_code_rejects_unknown_user(
    subscription: Subscription,
) -> None:
    (await subscription.code_svc.add_code("bonus1", "bonus"))
    with pytest.raises(NotFoundError):
        (await subscription.code_svc.apply_bonus_code(username="missing", code="bonus1"))


@pytest.mark.asyncio
async def test_register_with_code_creates_user(
    database: Database,
    subscription: Subscription,
) -> None:
    (await subscription.code_svc.add_code(
        "invite",
        "register",
        days=1,
        gb=2,
        wl_gb=3,
        uses=1,
    ))
    created = (await subscription.business_code_svc.register_with_code(
        code="invite",
        username="alice",
        displayname="Alice",
        ext_username="alice-login",
        ext_password="secret",
    ))
    assert created.username == "alice"
    assert created.limit == 2
    assert created.wl_limit == 3
    assert (await database.user_exists("alice"))
    assert (
        (await subscription.password_svc.validate_credentials(
            "alice-login",
            "secret",
        ))
        == "alice"
    )


@pytest.mark.asyncio
async def test_register_with_code_rejects_empty_code(
    subscription: Subscription,
) -> None:
    with pytest.raises(ValidationError):
        (await subscription.business_code_svc.register_with_code(
            code="",
            username="alice",
            displayname="Alice",
            ext_username="alice-login",
            ext_password="secret",
        ))


@pytest.mark.asyncio
async def test_register_with_code_rejects_panel_unsafe_username(
    database: Database,
    subscription: Subscription,
) -> None:
    (await subscription.code_svc.add_code("invite", "register", uses=1))
    for bad in ("bad name", "sl/ash", "back\\slash", ""):
        with pytest.raises(ValidationError):
            (await subscription.business_code_svc.register_with_code(
                code="invite",
                username=bad,
                displayname="Alice",
                ext_username="alice-login",
                ext_password="secret",
            ))
    assert not (await database.user_exists("bad name"))
    # the code was never consumed by a rejected attempt
    assert (await subscription.code_svc.get_code("invite")) is not None
