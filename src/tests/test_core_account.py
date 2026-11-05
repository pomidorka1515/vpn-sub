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
    async_db: Database,
    tmp_path: Path,
) -> None:
    sub = await make_async_subscription(async_db, tmp_path)
    try:
        await sub.business_svc.add_new_user(
            "alice",
            "Alice",
            ext_username="alice",
            ext_password="secret",
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
                "alice",
                ext_password="newsecret",
                current_password="wrong",
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
@pytest.mark.parametrize("mutation", ["password", "reset"])
async def test_login_coordinates_verification_and_session_issuance(
    async_db: Database,
    tmp_path: Path,
    mutation: str,
) -> None:
    sub = await make_async_subscription(async_db, tmp_path)
    verified = asyncio.Event()
    release = asyncio.Event()
    changed = asyncio.Event()
    validate = sub.password_svc.validate_credentials

    async def pause_after_verification(username: str, password: str) -> str | None:
        result = await validate(username, password)
        verified.set()
        await release.wait()
        return result

    async def change_account() -> None:
        if mutation == "password":
            await sub.business_svc.update_params("alice", ext_password="newsecret")
        else:
            await sub.business_svc.reset_user("alice")
        changed.set()

    try:
        await sub.business_svc.add_new_user(
            "alice",
            "Alice",
            ext_username="alice-login",
            ext_password="secret",
        )
        with patch.object(sub.password_svc, "validate_credentials", pause_after_verification):
            async with asyncio.TaskGroup() as tasks:
                login = tasks.create_task(sub.account_svc.login("alice-login", "secret"))
                await asyncio.wait_for(verified.wait(), timeout=5)
                tasks.create_task(change_account())
                try:
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(changed.wait(), timeout=0.1)
                finally:
                    release.set()
        assert changed.is_set()
        token = login.result()
        assert len(token) == 100
        if mutation == "reset":
            assert await sub.user_svc.auth_token_to_user(token) is None
        else:
            assert await sub.user_svc.auth_token_to_user(token) == "alice"
            assert await validate("alice-login", "secret") is None
    finally:
        release.set()
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_login_rejects_external_username_reassigned_before_lock(
    async_db: Database,
    tmp_path: Path,
) -> None:
    sub = await make_async_subscription(async_db, tmp_path)
    resolve = async_db.ext_to_user
    moved = False

    async def resolve_then_reassign(external: str) -> str | None:
        nonlocal moved
        internal = await resolve(external)
        if not moved:
            moved = True
            await sub.business_svc.update_params("alice", ext_username="alice-new")
            await sub.business_svc.update_params("bob", ext_username="alice-login")
        return internal

    try:
        for username in ("alice", "bob"):
            await sub.business_svc.add_new_user(
                username,
                username,
                ext_username=f"{username}-login",
                ext_password="secret",
            )
        with (
            patch.object(async_db, "ext_to_user", resolve_then_reassign),
            pytest.raises(AppError, match="Invalid credentials"),
        ):
            await sub.account_svc.login("alice-login", "secret")
        for username in ("alice", "bob"):
            assert (await sub.user_svc.user(username))["auth_token"] is None
    finally:
        await close_async_subscription(sub)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["settings", "delete"])
async def test_account_mutation_holds_lock_after_password_confirmation(
    async_db: Database,
    tmp_path: Path,
    operation: str,
) -> None:
    sub = await make_async_subscription(async_db, tmp_path)
    confirmed = asyncio.Event()
    release = asyncio.Event()
    changed = asyncio.Event()
    confirm = sub.account_svc.confirm_password

    async def pause_after_confirmation(username: str, password: str | None) -> None:
        await confirm(username, password)
        if not confirmed.is_set():
            confirmed.set()
            await release.wait()

    async def change_credentials() -> AppError | None:
        try:
            await sub.account_svc.settings(
                "alice",
                ext_password="intervening",
                current_password="secret",
            )
        except AppError as exc:
            return exc
        finally:
            changed.set()
        return None

    try:
        await sub.business_svc.add_new_user(
            "alice",
            "Alice",
            ext_username="alice",
            ext_password="secret",
        )
        with patch.object(sub.account_svc, "confirm_password", pause_after_confirmation):
            async with asyncio.TaskGroup() as tasks:
                if operation == "settings":
                    tasks.create_task(
                        sub.account_svc.settings(
                            "alice",
                            ext_password="newsecret",
                            current_password="secret",
                        ),
                    )
                else:
                    tasks.create_task(sub.account_svc.delete("alice", "secret"))
                await asyncio.wait_for(confirmed.wait(), timeout=5)
                competing = tasks.create_task(change_credentials())
                try:
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(changed.wait(), timeout=0.1)
                finally:
                    release.set()
        error = competing.result()
        assert isinstance(error, AppError)
        if operation == "settings":
            assert str(error) == "Invalid current password"
            assert await sub.password_svc.validate_credentials("alice", "newsecret") == "alice"
        else:
            assert isinstance(error, ValidationError)
            assert not await sub.user_svc.isuser("alice")
    finally:
        release.set()
        await close_async_subscription(sub)


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [{"ext_password": ""}, {"ext_username": ""}])
@pytest.mark.parametrize("current_password", [None, "", "wrong"])
async def test_empty_credentials_require_current_password(
    async_db: Database,
    tmp_path: Path,
    fields: dict[str, str],
    current_password: str | None,
) -> None:
    sub = await make_async_subscription(async_db, tmp_path)
    try:
        await sub.business_svc.add_new_user(
            "alice",
            "Alice",
            ext_username="alice",
            ext_password="secret",
        )
        token = await sub.account_svc.login("alice", "secret")
        before = await async_db.get_user("alice")
        error = "Invalid current password" if current_password else "current_password required"
        with pytest.raises(AppError, match=error):
            await sub.account_svc.settings(
                "alice",
                **fields,
                current_password=current_password,
            )
        assert await async_db.get_user("alice") == before
        assert await sub.user_svc.auth_token_to_user(token) == "alice"
        assert await sub.password_svc.validate_credentials("alice", "secret") == "alice"
        assert await sub.password_svc.validate_credentials("alice", "") is None
    finally:
        await close_async_subscription(sub)


@pytest.mark.asyncio
@pytest.mark.parametrize("password", ["", "x" * 129])
@pytest.mark.parametrize("operation", ["settings", "update", "create", "register"])
async def test_credential_mutations_reject_invalid_password_lengths(
    async_db: Database,
    tmp_path: Path,
    password: str,
    operation: str,
) -> None:
    sub = await make_async_subscription(async_db, tmp_path)
    try:
        await sub.business_svc.add_new_user(
            "alice",
            "Alice",
            ext_username="alice",
            ext_password="secret",
        )
        before = await async_db.get_user("alice")

        async def mutate() -> None:
            if operation == "settings":
                await sub.account_svc.settings(
                    "alice",
                    ext_password=password,
                    current_password="secret",
                    displayname="Changed",
                )
            elif operation == "update":
                await sub.business_svc.update_params(
                    "alice",
                    ext_password=password,
                    displayname="Changed",
                )
            elif operation == "create":
                await sub.business_svc.add_new_user(
                    "bob",
                    "Bob",
                    ext_username="bob",
                    ext_password=password,
                )
            else:
                await sub.business_code_svc.register_with_code(
                    code="unused",
                    username="bob",
                    displayname="Bob",
                    ext_username="bob",
                    ext_password=password,
                )

        with pytest.raises(ValidationError):
            await mutate()
        assert await async_db.get_user("alice") == before
        assert await async_db.get_user("bob") is None
        assert await sub.password_svc.validate_credentials("alice", "secret") == "alice"
    finally:
        await close_async_subscription(sub)


@pytest.mark.asyncio
@pytest.mark.parametrize("password", [" ", " padded password ", " " + "é" * 126 + " "])
@pytest.mark.parametrize("operation", ["settings", "update", "create"])
async def test_credential_mutations_preserve_valid_password_whitespace(
    async_db: Database,
    tmp_path: Path,
    password: str,
    operation: str,
) -> None:
    sub = await make_async_subscription(async_db, tmp_path)
    try:
        await sub.business_svc.add_new_user(
            "alice",
            "Alice",
            ext_username="alice",
            ext_password=password if operation == "create" else "secret",
        )
        if operation == "settings":
            await sub.account_svc.settings(
                "alice",
                ext_password=password,
                current_password="secret",
            )
        elif operation == "update":
            await sub.business_svc.update_params("alice", ext_password=password)
        assert await sub.password_svc.validate_credentials("alice", password) == "alice"
        assert await sub.password_svc.validate_credentials("alice", password.strip()) is None
    finally:
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_shared_account_delete_requires_password(async_db: Database, tmp_path: Path) -> None:
    sub = await make_async_subscription(async_db, tmp_path)
    try:
        await sub.business_svc.add_new_user(
            "alice",
            "Alice",
            ext_username="alice",
            ext_password="secret",
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
        {"displayname": "###"},
        {"displayname": "   "},
        {"ext_username": "a" * 33},
        {"ext_username": "!!!"},
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
    async_db: Database,
    tmp_path: Path,
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
    async_db: Database,
    tmp_path: Path,
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
