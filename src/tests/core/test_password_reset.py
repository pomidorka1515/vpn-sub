from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import pytest

from errors import NotFoundError, ValidationError
from tests.support.users import create_alice

if TYPE_CHECKING:
    from core import Subscription
    from db import Database

pytestmark = pytest.mark.asyncio


async def issue(database: Database, subscription: Subscription) -> str:
    await create_alice(
        database,
        ext_username="alice-login",
        ext_password_hash=await subscription.password_svc.hash("old-password"),
    )
    link = await subscription.account_svc.issue_password_reset("alice")
    assert urlsplit(link).path == "/sub/reset"
    token = parse_qs(urlsplit(link).query)["token"][0]
    assert re.fullmatch(r"[A-Za-z0-9_-]{75}", token)
    return token


async def test_password_reset_is_single_use_and_revokes_session(
    database: Database, subscription: Subscription
) -> None:
    token = await issue(database, subscription)
    await database.set_auth_token("alice", "a" * 100)
    before = await database.get_user("alice")
    await subscription.account_svc.reset_password(token, " new-password ")
    assert (
        await subscription.password_svc.validate_credentials("alice-login", " new-password ")
        == "alice"
    )
    assert (
        await subscription.password_svc.validate_credentials("alice-login", "old-password") is None
    )
    assert await database.auth_token_to_user("a" * 100) is None
    after = await database.get_user("alice")
    assert before is not None
    assert after is not None
    for field in ("uuid", "token", "ext_username"):
        assert after[field] == before[field]
    with pytest.raises(NotFoundError):
        await subscription.account_svc.reset_password(token, "another-password")


async def test_password_reset_reissue_and_expiration(
    database: Database, subscription: Subscription
) -> None:
    token = await issue(database, subscription)
    link = await subscription.account_svc.issue_password_reset("alice")
    replacement = parse_qs(urlsplit(link).query)["token"][0]
    assert replacement != token
    with pytest.raises(NotFoundError):
        await subscription.account_svc.password_reset(token)
    service = subscription.account_svc
    service._password_resets[replacement] = replace(
        service._password_resets[replacement], expires=0
    )
    with pytest.raises(NotFoundError):
        await service.reset_password(replacement, "new-password")


@pytest.mark.parametrize("token", [None, "", "invalid", "x" * 75])
async def test_unknown_reset_tokens_are_not_found(
    subscription: Subscription, token: str | None
) -> None:
    with pytest.raises(NotFoundError):
        await subscription.account_svc.password_reset(token)


async def test_invalid_password_preserves_reset_link(
    database: Database, subscription: Subscription
) -> None:
    token = await issue(database, subscription)
    for password in ("", "x" * 129):
        with pytest.raises(ValidationError):
            await subscription.account_svc.reset_password(token, password)
    await subscription.account_svc.reset_password(token, "valid-password")


async def test_credential_changes_invalidate_reset_link(
    database: Database, subscription: Subscription
) -> None:
    token = await issue(database, subscription)
    await subscription.business_svc.update_params("alice", ext_password="changed-password")
    with pytest.raises(NotFoundError):
        await subscription.account_svc.reset_password(token, "new-password")


async def test_concurrent_resets_only_commit_once(
    database: Database, subscription: Subscription
) -> None:
    token = await issue(database, subscription)
    results = await asyncio.gather(
        subscription.account_svc.reset_password(token, "first-password"),
        subscription.account_svc.reset_password(token, "second-password"),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, NotFoundError) for result in results) == 1


async def test_reset_requires_existing_web_account(
    database: Database, subscription: Subscription
) -> None:
    with pytest.raises(NotFoundError):
        await subscription.account_svc.issue_password_reset("missing")
    await create_alice(database)
    with pytest.raises(ValidationError):
        await subscription.account_svc.issue_password_reset("alice")
