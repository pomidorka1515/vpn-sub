from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import discord
import pytest
import pytest_asyncio

from bots.discord.sessions import DatabaseSessionStore, SessionRecord
from db import Database
from tests.support.discord import FakeInteraction, make_bots, modal_data

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from bots.discord.sessions import SessionStore


@pytest_asyncio.fixture
async def session_store(tmp_path: Path) -> AsyncIterator[DatabaseSessionStore]:
    db = Database(path=tmp_path / "state.sqlite3", start_backup=False)
    await db.start()
    store = DatabaseSessionStore(db)
    try:
        await store.start(tmp_path / "discord-sessions.json")
        yield store
    finally:
        try:
            await store.settle()
        finally:
            await db.close()


@pytest.mark.asyncio
async def test_session_access_and_clear_preserves_language(
    session_store: DatabaseSessionStore,
) -> None:
    sessions: SessionStore = session_store
    assert sessions.get(123) == SessionRecord("123", "", None)
    assert sessions.token(123) is None
    assert sessions.lang(123) is None

    sessions.set_lang("123", "en")
    sessions.set_token(123, "secret-token")
    assert sessions.token("123") == "secret-token"
    sessions.clear_token(123)
    assert sessions.get("123") == SessionRecord("123", "", "en")
    assert sessions.token(123) is None

    await session_store.flush()
    payload = await session_store.db.get_metadata("discord_sessions")
    assert payload is not None
    assert json.loads(payload) == {"123": {"token": "", "lang": "en"}}


@pytest.mark.asyncio
async def test_session_generation_changes_only_with_token_updates(
    session_store: DatabaseSessionStore,
) -> None:
    session_store.set_token(123, "token")
    generation = session_store.get(123).generation
    session_store.set_lang(123, "en")
    assert session_store.get(123).generation is generation
    session_store.set_token(123, "token")
    assert session_store.get(123).generation is not generation
    generation = session_store.get(123).generation
    session_store.clear_token(123)
    assert session_store.get(123).generation is not generation


@pytest.mark.asyncio
async def test_language_updates_preserve_token_and_ignore_invalid_language(
    session_store: DatabaseSessionStore,
) -> None:
    session_store.set_token(123, "secret-token")
    session_store.set_lang(123, "ru")
    session_store.set_lang(123, "de")
    session_store.set_lang(456, "de")
    assert session_store.get(123) == SessionRecord("123", "secret-token", "ru")
    assert session_store.get(456) == SessionRecord("456", "", None)
    await session_store.flush()
    payload = await session_store.db.get_metadata("discord_sessions")
    assert payload is not None
    assert json.loads(payload) == {"123": {"token": "secret-token", "lang": "ru"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [True, False])
async def test_session_writes_recover_after_failure(
    session_store: DatabaseSessionStore,
    monkeypatch: pytest.MonkeyPatch,
    queued: bool,
) -> None:
    set_metadata = session_store.db.set_metadata
    attempts = 0

    async def fail_once(key: str, value: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary persistence failure")
        await set_metadata(key, value)

    monkeypatch.setattr(session_store.db, "set_metadata", fail_once)
    session_store.set_token(123, "secret-token")
    if not queued:
        with pytest.raises(OSError, match="temporary persistence failure"):
            await session_store.flush()
    session_store.set_lang(123, "en")
    assert session_store.get(123) == SessionRecord("123", "secret-token", "en")

    await session_store.flush()
    assert attempts == 2
    payload = await session_store.db.get_metadata("discord_sessions")
    assert payload is not None
    assert json.loads(payload) == {"123": {"token": "secret-token", "lang": "en"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["valid", "expired", "replaced"])
@pytest.mark.parametrize(
    "action",
    ["start", "language", "login", "register", "login_modal", "register_modal"],
)
async def test_discord_authentication_checks_sqlite(
    session_store: DatabaseSessionStore,
    state: str,
    action: str,
) -> None:
    db = session_store.db
    await db.create_user(
        username="alice",
        uuid="uuid",
        token="subscription-token",
        fingerprint="chrome",
        displayname="Alice",
    )
    await db.set_auth_token("alice", "auth-token")
    session_store.set_lang(7, "en")
    session_store.set_token(7, "auth-token")
    if state == "expired":
        async with db.transaction(immediate=True) as conn:
            await conn.execute(
                "UPDATE users SET auth_token_expires_at = 0 WHERE username = ?",
                ("alice",),
            )
    elif state == "replaced":
        await db.set_auth_token("alice", "other-login-token")

    bot, _, _, sub = make_bots()
    bot.sessions = session_store
    sub.user_svc.auth_token_to_user = AsyncMock(side_effect=db.auth_token_to_user)

    async def login(username: str, password: str) -> str:
        await db.set_auth_token(username, "newtoken")
        return "newtoken"

    sub.account_svc.login.side_effect = login
    fake = FakeInteraction(
        data=modal_data(
            action,
            {"username": "alice", "password": "secret", "code": "INVITE", "name": "Ada"},
        ),
    )
    interaction = cast(discord.Interaction, fake)
    if action == "start":
        await bot.cmd_start(interaction)
    elif action == "language":
        await bot.set_lang_callback(interaction, "en")
    elif action == "login":
        await bot.cmd_login(interaction)
    elif action == "register":
        await bot.cmd_register(interaction)
    elif action == "login_modal":
        await bot.handle_login_modal(interaction)
    else:
        await bot.handle_register_modal(interaction)

    assert session_store.lang(7) == "en"
    if action in {"start", "language"}:
        view = cast(discord.ui.View, fake.response.messages[-1]["view"])
        ids = {item.custom_id for item in view.children if isinstance(item, discord.ui.Button)}
        assert ("menu_account" in ids) == (state == "valid")
        assert ("login_credentials" in ids) == (state != "valid")
    elif state == "valid":
        assert fake.response.messages[-1]["content"] == bot.text("en", "already_logged_in")
        assert not fake.response.modals
        sub.account_svc.login.assert_not_awaited()
    elif action.endswith("_modal"):
        sub.account_svc.login.assert_awaited_once_with("alice", "secret")
        assert session_store.token(7) == "newtoken"
    else:
        assert len(fake.response.modals) == 1

    if state != "valid" and not action.endswith("_modal"):
        assert session_store.token(7) is None
        await session_store.flush()
        payload = await db.get_metadata("discord_sessions")
        assert payload is not None
        assert json.loads(payload)["7"] == {"token": "", "lang": "en"}
