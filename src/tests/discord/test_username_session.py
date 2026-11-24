from __future__ import annotations

import asyncio
from typing import cast

import pytest

import discord
from bots.discord.exceptions import SessionExpiredError
from tests.support.discord import FakeInteraction, make_bots


@pytest.mark.asyncio
@pytest.mark.parametrize("new_token", ["other-account", "token"])
async def test_invalid_lookup_preserves_replacement_session(new_token: str) -> None:
    bot, _, store, sub = make_bots()
    entered, release = asyncio.Event(), asyncio.Event()

    async def lookup(token: str) -> None:
        assert token == "token"
        entered.set()
        await release.wait()

    sub.user_svc.auth_token_to_user.side_effect = lookup
    task = asyncio.create_task(bot.username(cast(discord.Interaction, FakeInteraction())))
    await asyncio.wait_for(entered.wait(), 1)
    store.set_token(7, new_token)
    generation = store.get(7).generation
    release.set()
    with pytest.raises(SessionExpiredError):
        await task
    assert store.token(7) == new_token
    assert store.get(7).generation is generation


@pytest.mark.asyncio
async def test_invalid_lookup_clears_unchanged_session_after_language_change() -> None:
    bot, _, store, sub = make_bots()
    entered, release = asyncio.Event(), asyncio.Event()

    async def lookup(token: str) -> None:
        entered.set()
        await release.wait()

    sub.user_svc.auth_token_to_user.side_effect = lookup
    task = asyncio.create_task(bot.username(cast(discord.Interaction, FakeInteraction())))
    await asyncio.wait_for(entered.wait(), 1)
    store.set_lang(7, "ru")
    release.set()
    with pytest.raises(SessionExpiredError):
        await task
    assert store.token(7) is None
    assert store.lang(7) == "ru"


@pytest.mark.asyncio
async def test_valid_lookup_returns_username_without_changing_session() -> None:
    bot, _, store, sub = make_bots()
    session = store.get(7)
    assert await bot.username(cast(discord.Interaction, FakeInteraction())) == "alice"
    sub.user_svc.auth_token_to_user.assert_awaited_once_with("token")
    assert store.get(7) is session


@pytest.mark.asyncio
async def test_missing_token_does_not_lookup_or_replace_session() -> None:
    bot, _, store, sub = make_bots()
    store.clear_token(7)
    session = store.get(7)
    with pytest.raises(SessionExpiredError):
        await bot.username(cast(discord.Interaction, FakeInteraction()))
    sub.user_svc.auth_token_to_user.assert_not_awaited()
    assert store.get(7) is session
