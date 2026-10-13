from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from helpers import make_subscription

from errors import ConflictError, DuplicateError, NotFoundError, ValidationError

if TYPE_CHECKING:
    from db import Database


@pytest.mark.asyncio
async def test_get_info_link_uses_configured_uri(database: Database) -> None:
    subscription = await make_subscription(database, uri="custom")
    created = await subscription.business_svc.add_new_user(
        "alice",
        "Alice",
        ext_username="alice-login",
        ext_password="secret",
        limit=10,
        wl_limit=5,
    )
    info = await subscription.business_svc.get_info("alice")
    assert info.link == f"https://example.test/custom?token={created.token}"


@pytest.mark.asyncio
async def test_user_workflow_without_live_panels(database: Database) -> None:
    subscription = await make_subscription(
        database,
        fingerprints=["chrome", "firefox"],
    )
    with pytest.raises(NotFoundError):
        (await subscription.business_svc.get_info("missing"))
    created = await subscription.business_svc.add_new_user(
        "alice",
        "Alice",
        ext_username="alice-login",
        ext_password="secret",
        limit=10,
        wl_limit=5,
    )
    with pytest.raises(ValidationError):
        (await subscription.business_svc.update_params("alice", fingerprint="invalid"))
    with pytest.raises(ConflictError) as user_conflict:
        (
            await subscription.business_svc.add_new_user(
                "alice",
                "Alice",
                ext_username="alice-login",
                ext_password="secret",
                limit=10,
                wl_limit=5,
            )
        )
    assert isinstance(user_conflict.value.__cause__, DuplicateError)
    with pytest.raises(ValidationError):
        (await subscription.code_svc.add_code("", "bonus"))
    (await subscription.code_svc.add_code("invite", "bonus"))
    with pytest.raises(ConflictError) as code_conflict:
        (await subscription.code_svc.add_code("invite", "bonus"))
    assert isinstance(code_conflict.value.__cause__, DuplicateError)
    with pytest.raises(NotFoundError):
        (await subscription.code_svc.get_code("missing"))
    with pytest.raises(NotFoundError):
        (await subscription.code_svc.delete_code("missing"))
    assert created.username == "alice"
    assert (
        await subscription.password_svc.validate_credentials("alice-login", "secret")
    ) == "alice"
    (await subscription.telegram_svc.set_telegram_user(123, "alice"))
    assert (await subscription.telegram_svc.get_username_telegram(123)) == "alice"
    (
        await subscription.business_svc.update_params(
            "alice", displayname="Alice 2", fingerprint="firefox"
        )
    )
    info = await subscription.business_svc.get_info("alice")
    assert (info.displayname, info.fingerprint) == ("Alice 2", "firefox")
    assert info.link == f"https://example.test/sub?token={created.token}"
    (await subscription.business_svc.delete_user("alice", perma=True))
    assert not (await subscription.user_svc.isuser("alice"))


@pytest.mark.asyncio
async def test_external_username_conflict_preserves_cause(database: Database) -> None:
    subscription = await make_subscription(database)
    (
        await subscription.business_svc.add_new_user(
            "alice",
            "Alice",
            ext_username="alice-login",
            ext_password="secret",
        )
    )
    (await subscription.business_svc.add_new_user("bob", "Bob"))

    with pytest.raises(ConflictError) as conflict:
        (await subscription.business_svc.update_params("bob", ext_username="alice-login"))

    assert isinstance(conflict.value.__cause__, DuplicateError)
