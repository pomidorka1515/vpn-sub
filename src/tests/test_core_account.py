from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
import pytest_asyncio
from async_core_helpers import close_async_subscription, make_async_subscription

from core.services.refresh import RefreshResult
from db import Database
from errors import AppError, PanelUnavailableError, ValidationError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest_asyncio.fixture
async def async_db(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(path=tmp_path / "state.sqlite3", start_backup=False)
    await db.start()
    try:
        yield db
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_shared_login_logout_credentials_and_reset(
    async_db: Database, tmp_path: Path
) -> None:
    sub = await make_async_subscription(async_db, tmp_path)
    try:
        await sub.business_svc.add_new_user(
            "alice", "Alice", ext_username="alice", ext_password="secret"
        )
        with pytest.raises(AppError, match="Invalid credentials"):
            await sub.account_svc.login("alice", "wrong")
        token = await sub.account_svc.login("alice", "secret")
        assert len(token) == 100
        assert await sub.user_svc.auth_token_to_user(token) == "alice"
        replacement = await sub.account_svc.login("alice", "secret")
        assert replacement != token
        assert await sub.user_svc.auth_token_to_user(token) is None
        with pytest.raises(ValidationError, match="current_password required"):
            await sub.account_svc.settings("alice", ext_password="newsecret")
        with pytest.raises(AppError, match="Invalid current password"):
            await sub.account_svc.settings(
                "alice", ext_password="newsecret", current_password="wrong"
            )
        assert await sub.user_svc.auth_token_to_user(replacement) == "alice"
        await sub.account_svc.settings("alice", ext_password="newsecret", current_password="secret")
        assert await sub.user_svc.auth_token_to_user(replacement) == "alice"
        assert await sub.password_svc.validate_credentials("alice", "secret") is None
        token = await sub.account_svc.login("alice", "newsecret")
        await sub.account_svc.logout("alice")
        assert await sub.user_svc.auth_token_to_user(token) is None
        token = await sub.account_svc.login("alice", "newsecret")
        await sub.business_svc.reset_user("alice")
        assert await sub.user_svc.auth_token_to_user(token) is None
    finally:
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_shared_account_delete_requires_password(async_db: Database, tmp_path: Path) -> None:
    sub = await make_async_subscription(async_db, tmp_path)
    try:
        await sub.business_svc.add_new_user(
            "alice", "Alice", ext_username="alice", ext_password="secret"
        )
        token = await sub.account_svc.login("alice", "secret")
        with pytest.raises(AppError, match="Invalid current password"):
            await sub.account_svc.delete("alice", "wrong")
        assert await sub.user_svc.auth_token_to_user(token) == "alice"
        await sub.account_svc.delete("alice", "secret")
        assert not await sub.user_svc.isuser("alice")
        assert await sub.user_svc.auth_token_to_user(token) is None
    finally:
        await close_async_subscription(sub)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"displayname": "a" * 17},
        {"ext_username": "a" * 33},
        {"fingerprint": "unknown"},
    ],
)
async def test_account_settings_preserve_route_validation(
    async_db: Database,
    tmp_path: Path,
    fields: dict[str, str],
) -> None:
    sub = await make_async_subscription(async_db, tmp_path)
    try:
        await sub.business_svc.add_new_user("alice", "Alice")
        with pytest.raises(ValidationError):
            await sub.account_svc.settings("alice", **fields)
        assert (await sub.user_svc.user("alice"))["displayname"] == "Alice"
    finally:
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_bulk_refresh_bounds_concurrency_and_preserves_failure_order(
    async_db: Database, tmp_path: Path
) -> None:
    sub = await make_async_subscription(async_db, tmp_path)
    active = peak = 0
    names = [f"user{index}" for index in range(9)]

    async def refresh(username: str) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0)
            if username in {"user1", "user5"}:
                raise PanelUnavailableError
        finally:
            active -= 1

    try:
        with (
            patch.object(sub.user_svc, "list_users", return_value=names),
            patch.object(sub.business_svc, "add_users", side_effect=refresh),
        ):
            assert await sub.refresh_svc.refresh() == RefreshResult(["user1", "user5"], None, 7, 9)
        assert peak == 4
        assert active == 0
    finally:
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_bulk_refresh_aborts_new_claims_and_drains_inflight(
    async_db: Database, tmp_path: Path
) -> None:
    sub = await make_async_subscription(async_db, tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    claimed: list[str] = []

    async def refresh(username: str) -> None:
        claimed.append(username)
        if len(claimed) == 4:
            entered.set()
        await entered.wait()
        if username == "user0":
            release.set()
            raise RuntimeError("abort")
        await release.wait()

    try:
        with (
            patch.object(sub.user_svc, "list_users", return_value=[f"user{i}" for i in range(8)]),
            patch.object(sub.business_svc, "add_users", side_effect=refresh),
        ):
            result = await sub.refresh_svc.refresh()
        assert result == RefreshResult([], "user0", 3, 8)
        assert claimed == ["user0", "user1", "user2", "user3"]
    finally:
        await close_async_subscription(sub)
