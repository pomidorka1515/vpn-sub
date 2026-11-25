from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest

import discord
from bots.discord.host import interaction_limit
from bots.discord.public import account
from tests.support.discord import FakeInteraction, make_bots

if TYPE_CHECKING:
    from bots.discord.public import DiscordPublicBot


async def prompt(bot: DiscordPublicBot, action: str) -> str:
    fake = FakeInteraction()
    interaction = cast(discord.Interaction, fake)
    if action == "reset":
        await bot.cmd_reset(interaction)
    else:
        await bot.cmd_logout(interaction)
    view = cast(discord.ui.View, fake.response.messages[-1]["view"])
    button = cast(discord.ui.Button[discord.ui.View], view.children[0])
    assert button.custom_id is not None
    return button.custom_id


async def click(bot: DiscordPublicBot, custom_id: str, user_id: int = 7) -> FakeInteraction:
    fake = FakeInteraction(user_id, data={"custom_id": custom_id})
    await bot.dispatch_component(cast(discord.Interaction, fake))
    return fake


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reset", "logout"])
@pytest.mark.parametrize("new_token", ["other-account", "token"])
async def test_session_switch_or_same_account_relogin_invalidates_prompt(
    action: str,
    new_token: str,
) -> None:
    bot, _, store, sub = make_bots()
    custom_id = await prompt(bot, action)
    store.set_token(7, new_token)
    await click(bot, custom_id)
    sub.business_svc.reset_user.assert_not_awaited()
    sub.account_svc.logout.assert_not_awaited()
    assert store.token(7) == new_token


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reset", "logout"])
async def test_valid_confirmation_is_single_use_even_with_concurrent_clicks(action: str) -> None:
    bot, _, store, sub = make_bots()
    custom_id = await prompt(bot, action)
    entered, release = asyncio.Event(), asyncio.Event()

    async def lookup(token: str) -> str:
        assert token == "token"
        entered.set()
        await release.wait()
        return "alice"

    sub.user_svc.auth_token_to_user.side_effect = lookup
    first = asyncio.create_task(click(bot, custom_id))
    await asyncio.wait_for(entered.wait(), 1)
    await click(bot, custom_id)
    blocked = FakeInteraction()
    await bot.cmd_reset(cast(discord.Interaction, blocked))
    assert "view" not in blocked.response.messages[-1]
    release.set()
    await first
    await click(bot, custom_id)
    if action == "reset":
        sub.business_svc.reset_user.assert_awaited_once_with("alice")
        sub.account_svc.logout.assert_not_awaited()
    else:
        sub.account_svc.logout.assert_awaited_once_with("alice")
        sub.business_svc.reset_user.assert_not_awaited()
    assert store.token(7) is None
    assert store.lang(7) == "en"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reset", "logout"])
async def test_expired_confirmation_does_not_mutate(
    action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, _, store, sub = make_bots()
    monkeypatch.setattr(account, "_CONFIRM_TTL", 0.0)
    custom_id = await prompt(bot, action)
    await click(bot, custom_id)
    sub.business_svc.reset_user.assert_not_awaited()
    sub.account_svc.logout.assert_not_awaited()
    assert store.token(7) == "token"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reset", "logout"])
async def test_session_change_during_lookup_does_not_mutate(action: str) -> None:
    bot, _, store, sub = make_bots()
    custom_id = await prompt(bot, action)

    async def lookup(token: str) -> str:
        store.set_token(7, token)
        return "alice"

    sub.user_svc.auth_token_to_user.side_effect = lookup
    await click(bot, custom_id)
    sub.business_svc.reset_user.assert_not_awaited()
    sub.account_svc.logout.assert_not_awaited()
    assert store.token(7) == "token"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reset", "logout"])
async def test_session_change_during_mutation_does_not_clear_new_session(action: str) -> None:
    bot, _, store, sub = make_bots()
    custom_id = await prompt(bot, action)

    async def mutate(username: str) -> None:
        assert username == "alice"
        store.set_token(7, "other-account")

    sub.business_svc.reset_user.side_effect = mutate
    sub.account_svc.logout.side_effect = mutate
    await click(bot, custom_id)
    assert store.token(7) == "other-account"


@pytest.mark.asyncio
async def test_confirmation_is_bound_to_user_and_latest_prompt_and_can_be_cancelled() -> None:
    bot, _, store, sub = make_bots()
    first = await prompt(bot, "reset")
    second = await prompt(bot, "logout")
    assert first != second
    store.set_token(8, "token")
    await click(bot, second, user_id=8)
    await click(bot, first)
    await click(bot, second.replace("confirm_logout:", "confirm_reset:"))
    await click(bot, "confirm_reset")
    await click(bot, "confirm_logout")
    await click(bot, second.replace("confirm_logout:", "confirm_cancel:"))
    await click(bot, second)
    sub.business_svc.reset_user.assert_not_awaited()
    sub.account_svc.logout.assert_not_awaited()
    assert store.token(7) == "token"
    assert store.token(8) == "token"


def test_confirmation_rate_limits_ignore_nonce() -> None:
    assert interaction_limit("confirm_reset:first") == ("reset", 3)
    assert interaction_limit("confirm_reset:second") == ("reset", 3)
    assert interaction_limit("confirm_logout:first") == ("logout", 20)
    assert interaction_limit("confirm_cancel:first") == ("confirm_cancel", 20)


@pytest.mark.asyncio
async def test_language_change_preserves_confirmation() -> None:
    bot, _, store, sub = make_bots()
    custom_id = await prompt(bot, "reset")
    store.set_lang(7, "ru")
    await click(bot, custom_id)
    sub.business_svc.reset_user.assert_awaited_once_with("alice")
