from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from errors import ConflictError, NotFoundError, ValidationError
from tests.support.users import create_alice

if TYPE_CHECKING:
    from core import Subscription
    from db import Database


@pytest.mark.asyncio
async def test_add_code_rejects_unknown_action(subscription: Subscription) -> None:
    with pytest.raises(ValidationError):
        (await subscription.code_svc.add_code("invite", "gift"))


@pytest.mark.asyncio
async def test_list_and_get_code_round_trip(subscription: Subscription) -> None:
    (
        await subscription.code_svc.add_code(
            "bonus1",
            "bonus",
            days=2,
            gb=3,
            wl_gb=4,
            uses=2,
        )
    )
    assert (await subscription.code_svc.list_code()) == ["bonus1"]
    code = await subscription.code_svc.get_code("bonus1")
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
    (
        await subscription.code_svc.add_code(
            "bonus1",
            "bonus",
            days=1,
            gb=3,
            wl_gb=4,
            uses=1,
        )
    )
    result = await subscription.code_svc.apply_bonus_code(username="alice", code="bonus1")
    record = await database.get_user("alice")
    assert record is not None
    assert result.gb == 3
    assert result.limit == 4
    assert result.wl_limit == 6
    assert record["bw_limit_gb"] == 4
    assert record["wl_limit_gb"] == 6
    with pytest.raises(NotFoundError):
        (await subscription.code_svc.get_code("bonus1"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("main_limit", "wl_limit", "expected_main", "expected_wl"),
    [(0, 0, 0, 0), (0, 2, 0, 6), (1, 0, 4, 0)],
)
async def test_apply_bonus_code_preserves_unlimited_allowances(
    database: Database,
    subscription: Subscription,
    main_limit: int,
    wl_limit: int,
    expected_main: int,
    expected_wl: int,
) -> None:
    (await create_alice(database, bw_limit_gb=main_limit, wl_limit_gb=wl_limit, expires_at=0))
    (await database.update_user("alice", status=False, status_time=False, status_wl=False))
    (
        await subscription.code_svc.add_code(
            "bonus1",
            "bonus",
            days=0,
            gb=3,
            wl_gb=4,
            uses=1,
        )
    )
    result = await subscription.code_svc.apply_bonus_code(username="alice", code="bonus1")
    record = await database.get_user("alice")
    assert record is not None
    assert result.limit == expected_main
    assert result.wl_limit == expected_wl
    assert record["bw_limit_gb"] == expected_main
    assert record["wl_limit_gb"] == expected_wl
    assert record["expires_at"] == 0
    with pytest.raises(NotFoundError):
        await subscription.code_svc.get_code("bonus1")


@pytest.mark.asyncio
async def test_bonus_expiry_overflow_rolls_back_code_and_user(
    database: Database, subscription: Subscription
) -> None:
    await create_alice(database, expires_at=2**63 - 1)
    await subscription.code_svc.add_code("bonus1", "bonus", days=1)
    with pytest.raises(ValidationError, match="Invalid timestamp"):
        await subscription.code_svc.apply_bonus_code(username="alice", code="bonus1")
    user = await database.get_user("alice")
    assert user is not None
    assert user["expires_at"] == 2**63 - 1
    assert (await subscription.code_svc.get_code("bonus1")).uses == 1


@pytest.mark.asyncio
async def test_registration_expiry_overflow_rolls_back_code(database: Database) -> None:
    await database.add_code("invite", "register", days=2**63 // 86400)
    with pytest.raises(ValidationError, match="Invalid timestamp"):
        await database.register_with_code(
            code="invite",
            username="alice",
            uuid="alice",
            token="alice",
            fingerprint="chrome",
            displayname="Alice",
            ext_username="alice",
            ext_password_hash="hash",
        )
    assert await database.get_user("alice") is None
    code = await database.get_code("invite")
    assert code is not None
    assert code["uses"] == 1


@pytest.mark.asyncio
async def test_apply_bonus_code_restarts_lapsed_expiry(
    database: Database,
    subscription: Subscription,
) -> None:
    (await create_alice(database, bw_limit_gb=1, wl_limit_gb=1, expires_at=1))
    (await database.update_user("alice", status=False, status_time=False))
    (
        await subscription.code_svc.add_code(
            "bonus1",
            "bonus",
            days=2,
            gb=0,
            wl_gb=0,
            uses=1,
        )
    )
    before = int(time.time())
    result = await subscription.code_svc.apply_bonus_code(username="alice", code="bonus1")
    after = int(time.time())
    record = await database.get_user("alice")
    assert record is not None
    assert before + 2 * 86400 <= result.time <= after + 2 * 86400
    assert record["expires_at"] == result.time
    assert record["bw_limit_gb"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pending",
    [
        "registration",
        "registration_rollback_failed",
        "user_create_pending",
        "user_delete_pending",
        "user_reset_pending",
        "uuid_rollback_failed",
    ],
)
async def test_apply_bonus_code_rejects_pending_recovery_without_consuming_code(
    database: Database,
    subscription: Subscription,
    pending: str,
) -> None:
    await create_alice(database, bw_limit_gb=1, wl_limit_gb=2, expires_at=1000)
    await subscription.code_svc.add_code("bonus1", "bonus", days=1, gb=3, wl_gb=4)
    if pending == "registration":
        await database.add_code("invite", "register")
        async with database.transaction(immediate=True) as conn:
            await conn.execute(
                "INSERT INTO pending_registration_sync(username, code, permanent) VALUES (?, ?, ?)",
                ("alice", "invite", 0),
            )
    else:
        await database.set_metadata(f"{pending}:alice", "123")
    before = await database.get_user("alice")

    with pytest.raises(ConflictError):
        await subscription.code_svc.apply_bonus_code(username="alice", code="bonus1")

    assert await database.get_user("alice") == before
    assert (await subscription.code_svc.get_code("bonus1")).uses == 1
    if pending == "registration":
        assert await database.registration_pending("alice")
        await database.confirm_registration_sync("alice")
    else:
        assert await database.get_metadata(f"{pending}:alice") == "123"
        await database.delete_metadata(f"{pending}:alice")

    result = await subscription.code_svc.apply_bonus_code(username="alice", code="bonus1")
    assert (result.limit, result.wl_limit) == (4, 6)
    with pytest.raises(NotFoundError):
        await subscription.code_svc.get_code("bonus1")


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
    (
        await subscription.code_svc.add_code(
            "invite",
            "register",
            days=1,
            gb=2,
            wl_gb=3,
            uses=1,
        )
    )
    created = await subscription.business_code_svc.register_with_code(
        code="invite",
        username="alice",
        displayname="Alice",
        ext_username="alice-login",
        ext_password="secret",
    )
    assert created.username == "alice"
    assert created.limit == 2
    assert created.wl_limit == 3
    assert await database.user_exists("alice")
    assert (
        await subscription.password_svc.validate_credentials(
            "alice-login",
            "secret",
        )
    ) == "alice"


@pytest.mark.asyncio
async def test_register_with_code_rejects_empty_code(
    subscription: Subscription,
) -> None:
    with pytest.raises(ValidationError):
        (
            await subscription.business_code_svc.register_with_code(
                code="",
                username="alice",
                displayname="Alice",
                ext_username="alice-login",
                ext_password="secret",
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("displayname", "external"),
    [
        ("###", "alice"),
        ("   ", "alice"),
        ("Alice", "!!!"),
        ("Alice", "a" * 33),
        ("Alice", "a!lice"),
        ("Alice", " alice "),
        ("Alice", "alíce"),
    ],
)
async def test_register_rejects_invalid_names_without_consuming_code(
    database: Database,
    subscription: Subscription,
    displayname: str,
    external: str,
) -> None:
    await subscription.code_svc.add_code("invite", "register", uses=1)
    with pytest.raises(ValidationError):
        await subscription.business_code_svc.register_with_code(
            code="invite",
            username="alice",
            displayname=displayname,
            ext_username=external,
            ext_password="secret",
        )
    assert not await database.user_exists("alice")
    assert (await subscription.code_svc.get_code("invite")).uses == 1


@pytest.mark.asyncio
async def test_register_with_code_rejects_panel_unsafe_username(
    database: Database,
    subscription: Subscription,
) -> None:
    (await subscription.code_svc.add_code("invite", "register", uses=1))
    for bad in ("bad name", "sl/ash", "back\\slash", ""):
        with pytest.raises(ValidationError):
            (
                await subscription.business_code_svc.register_with_code(
                    code="invite",
                    username=bad,
                    displayname="Alice",
                    ext_username="alice-login",
                    ext_password="secret",
                )
            )
    assert not (await database.user_exists("bad name"))
    # the code was never consumed by a rejected attempt
    assert (await subscription.code_svc.get_code("invite")) is not None
