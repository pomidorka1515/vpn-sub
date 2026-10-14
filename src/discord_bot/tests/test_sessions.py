from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from db import Database
from discord_bot.sessions import DatabaseSessionStore, SessionRecord

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from discord_bot.sessions import SessionStore


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
    assert not sessions.logged_in(123)
    assert sessions.token(123) is None
    assert sessions.lang(123) is None

    sessions.set_lang("123", "en")
    sessions.set_token(123, "secret-token")
    assert sessions.logged_in("123")
    assert sessions.token("123") == "secret-token"
    sessions.clear_token(123)
    assert sessions.get("123") == SessionRecord("123", "", "en")
    assert not sessions.logged_in(123)
    assert sessions.token(123) is None

    await session_store.flush()
    payload = await session_store.db.get_metadata("discord_sessions")
    assert payload is not None
    assert json.loads(payload) == {"123": {"token": "", "lang": "en"}}


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
