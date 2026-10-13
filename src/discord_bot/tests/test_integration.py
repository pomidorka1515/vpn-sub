from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from quart import jsonify, request

from api.application import create_api_application
from db import Database
from discord_bot.host import SharedDiscordClient, discord_user
from discord_bot.sessions import DatabaseSessionStore
from discord_bot.transport import LocalTransport

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from quart import Response


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(path=tmp_path / "state.sqlite3", start_backup=False)
    await db.start()
    try:
        yield db
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_dispatch_preserves_identity_cookies_and_errors() -> None:
    app = create_api_application(require_proxy=True)

    @app.post("/login")
    async def login() -> Response:
        assert request.remote_addr == "discord:42"
        assert await request.get_json() == {"password": "secret"}
        response = jsonify(success=True, obj={"user": "alice"})
        response.set_cookie("auth_token", "token")
        return response

    transport = LocalTransport(app)
    identity = discord_user.set("42")
    try:
        result = await transport.request("POST", "/login", headers={}, json={"password": "secret"})
        assert result.ok
        assert result.auth_token == "token"
        missing = await transport.request("GET", "/missing", headers={})
        assert not missing.ok
        assert missing.status == 404
    finally:
        discord_user.reset(identity)


@pytest.mark.asyncio
async def test_sessions_import_once_and_persist(database: Database, tmp_path: Path) -> None:
    legacy = tmp_path / "discord-sessions.json"
    legacy.write_text(json.dumps({"42": {"token": "old", "lang": "en"}}))
    store = DatabaseSessionStore(database)
    await store.start(legacy)
    assert store.token(42) == "old"
    store.set_token(42, "new")
    store.set_lang(42, "ru")
    await store.flush()
    restored = DatabaseSessionStore(database)
    await restored.start(legacy)
    assert restored.token(42) == "new"
    assert restored.lang(42) == "ru"
    restored.clear_token(42)
    await restored.flush()
    assert restored.token(42) is None
    assert legacy.exists()


@pytest.mark.asyncio
async def test_discord_quiesces_admitted_commands() -> None:
    import discord

    client = SharedDiscordClient(intents=discord.Intents.none())
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def command() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    task = asyncio.create_task(client.process("7", command))
    await entered.wait()
    await client.close()
    await asyncio.gather(task, return_exceptions=True)
    assert finished.is_set()
    assert client.handlers.active_count == 0
