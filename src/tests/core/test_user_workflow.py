from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest

from errors import ConflictError, DuplicateError, NotFoundError, ValidationError
from tests.support.subscriptions import make_subscription

if TYPE_CHECKING:
    from db import Database


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token",
    ["", "short", "a" * 29, *["a" * 30 + char for char in "&=?#%+ /\n\ré"]],
)
async def test_custom_token_validation_before_mutation(database: Database, token: str) -> None:
    subscription = await make_subscription(database)
    with patch.object(subscription.business_svc, "add_users") as add_users:
        with pytest.raises(ValidationError, match="Subscription token"):
            await subscription.business_svc.add_new_user("alice", "Alice", token=token)
        add_users.assert_not_called()
    assert not await subscription.user_svc.isuser("alice")

    created = await subscription.business_svc.add_new_user("alice", "Alice")
    with patch.object(subscription.business_svc, "_drop_cache") as drop_cache:
        with pytest.raises(ValidationError, match="Subscription token"):
            await subscription.business_svc.update_params(
                "alice", displayname="Changed", token=token
            )
        drop_cache.assert_not_called()
    user = await subscription.user_svc.user("alice")
    assert user["token"] == created.token
    assert user["displayname"] == "Alice"
    assert await subscription.user_svc.usertotoken(created.token) == "alice"
    assert await subscription.user_svc.usertotoken(token) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["a" * 30, "Az09._~-" * 5])
async def test_custom_tokens_round_trip_subscription_links(database: Database, token: str) -> None:
    subscription = await make_subscription(database)
    created = await subscription.business_svc.add_new_user("alice", "Alice", token=token)
    assert created.token == token
    for current_token in (token, token + "updated"):
        if current_token != token:
            await subscription.business_svc.update_params("alice", token=current_token)
            assert await subscription.user_svc.usertotoken(token) is None
        info = await subscription.business_svc.get_info("alice")
        link = urlsplit(info.link)
        assert not link.fragment
        assert parse_qs(link.query) == {"token": [current_token]}
        assert await subscription.user_svc.usertotoken(parse_qs(link.query)["token"][0]) == "alice"


@pytest.mark.asyncio
@pytest.mark.parametrize("ext_username", [None, ""])
@pytest.mark.parametrize("ext_password", ["secret", ""])
async def test_creation_requires_external_username_for_password(
    database: Database,
    ext_username: str | None,
    ext_password: str,
) -> None:
    subscription = await make_subscription(database)
    with patch.object(subscription.password_svc, "hash") as hash_password:
        with pytest.raises(ValidationError, match="External username required"):
            await subscription.business_svc.add_new_user(
                "alice",
                "Alice",
                ext_username=ext_username,
                ext_password=ext_password,
            )
        hash_password.assert_not_called()
    assert not await subscription.user_svc.isuser("alice")


@pytest.mark.asyncio
@pytest.mark.parametrize("ext_username", ["   ", "!!!"])
async def test_creation_rejects_external_username_empty_after_sanitizing(
    database: Database,
    ext_username: str,
) -> None:
    subscription = await make_subscription(database)
    with patch.object(subscription.password_svc, "hash") as hash_password:
        with pytest.raises(ValidationError, match="Invalid external name"):
            await subscription.business_svc.add_new_user(
                "alice",
                "Alice",
                ext_username=ext_username,
                ext_password="secret",
            )
        hash_password.assert_not_called()
    assert not await subscription.user_svc.isuser("alice")


@pytest.mark.asyncio
async def test_creation_without_external_credentials(database: Database) -> None:
    subscription = await make_subscription(database)
    await subscription.business_svc.add_new_user("alice", "Alice")
    user = await subscription.user_svc.user("alice")
    assert user["ext_username"] is None
    assert await database.ext_password("alice") is None


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
            "alice",
            displayname="Alice 2",
            fingerprint="firefox",
        )
    )
    info = await subscription.business_svc.get_info("alice")
    assert (info.displayname, info.fingerprint) == ("Alice 2", "firefox")
    assert info.link == f"https://example.test/sub?token={created.token}"
    (await subscription.business_svc.delete_user("alice"))
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
