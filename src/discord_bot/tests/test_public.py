from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

import discord
from discord_bot.exceptions import SessionExpiredError
from discord_bot.tests.discord_helpers import FakeInteraction, make_bots, modal_data
from errors import AppError


@pytest.mark.asyncio
@pytest.mark.parametrize("guild_id", [None, 123])
async def test_info_resolves_persisted_token_and_reads_fresh_stats(guild_id: int | None) -> None:
    bot, _, _, sub = make_bots()
    for _ in range(2):
        fake = FakeInteraction(guild_id=guild_id)
        await bot.cmd_info(cast(discord.Interaction, fake))
        assert fake.response.deferred
        assert "alice" in str(fake.response.messages[-1]["content"])
        assert fake.response.defers[-1]["ephemeral"] == (guild_id is not None)
    assert sub.business_svc.get_info.await_count == 2
    sub.user_svc.auth_token_to_user.assert_awaited_with("token")


@pytest.mark.asyncio
async def test_subscription_uses_shared_qr_rendering() -> None:
    bot, _, _, sub = make_bots()
    fake = FakeInteraction(guild_id=123)
    await bot.cmd_sub(cast(discord.Interaction, fake))
    sub.business_svc.get_info.assert_awaited_once_with("alice")
    assert fake.response.messages[-1]["file"] is not None
    assert fake.response.messages[-1]["ephemeral"] is True
    await bot.rendering.close()


@pytest.mark.asyncio
async def test_registration_then_shared_login_persists_auth_token() -> None:
    bot, _, store, sub = make_bots()
    store.clear_token(7)
    fake = FakeInteraction(data=modal_data("register_modal", {
        "username": "alice", "password": "secret", "code": "INVITE", "name": "Ada",
    }))
    await bot.handle_register_modal(cast(discord.Interaction, fake))
    fields = sub.business_code_svc.register_with_code.await_args.kwargs
    assert fields["username"].startswith("web_")
    assert fields["ext_username"] == "alice"
    assert fields["ext_password"] == "secret"
    assert fields["code"] == "INVITE"
    sub.account_svc.login.assert_awaited_once_with("alice", "secret")
    assert store.token(7) == "newtoken"


@pytest.mark.asyncio
@pytest.mark.parametrize("username", ["", "bad name", "a" * 33])
async def test_registration_rejects_invalid_username(username: str) -> None:
    bot, _, store, sub = make_bots()
    store.clear_token(7)
    fake = FakeInteraction(data=modal_data("register_modal", {
        "username": username, "password": "secret", "code": "INVITE", "name": "Ada",
    }))
    await bot.handle_register_modal(cast(discord.Interaction, fake))
    sub.business_code_svc.register_with_code.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_invalid_credentials_does_not_persist_session() -> None:
    bot, _, store, sub = make_bots()
    store.clear_token(7)
    sub.account_svc.login.side_effect = AppError("Invalid credentials.", status=401)
    fake = FakeInteraction(data=modal_data("login_modal", {"username": "alice", "password": "wrong"}))
    await bot.handle_login_modal(cast(discord.Interaction, fake))
    assert not store.logged_in(7)
    assert fake.response.messages


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["logout", "reset", "delete"])
async def test_account_operations_clear_discord_session(operation: str) -> None:
    bot, _, store, sub = make_bots()
    fake = FakeInteraction(data=modal_data("delete_modal", {"current_password": "secret"}))
    interaction = cast(discord.Interaction, fake)
    if operation == "logout":
        await bot.confirm_logout(interaction)
        sub.account_svc.logout.assert_awaited_once_with("alice")
    elif operation == "reset":
        await bot.confirm_reset(interaction)
        sub.business_svc.reset_user.assert_awaited_once_with("alice")
    else:
        await bot.handle_delete_modal(interaction)
        sub.account_svc.delete.assert_awaited_once_with("alice", "secret")
    assert not store.logged_in(7)
    assert store.lang(7) == "en"


@pytest.mark.asyncio
async def test_settings_pass_password_confirmation_to_shared_workflow() -> None:
    bot, _, _, sub = make_bots()
    fake = FakeInteraction(data=modal_data("settings_login_modal", {"username": "newname", "current_password": "secret"}))
    await bot.handle_login_change_modal(cast(discord.Interaction, fake))
    sub.account_svc.settings.assert_awaited_once_with("alice", ext_username="newname", current_password="secret")


@pytest.mark.asyncio
async def test_expired_session_clears_only_discord_token() -> None:
    bot, _, store, sub = make_bots()
    sub.user_svc.auth_token_to_user.return_value = None
    with pytest.raises(SessionExpiredError):
        await bot.cmd_info(cast(discord.Interaction, FakeInteraction()))
    assert store.token(7) is None
    sub.business_svc.get_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_bonus_refreshes_info_and_chart_uses_typed_snapshots() -> None:
    bot, _, _, sub = make_bots()
    fake = FakeInteraction(data=modal_data("bonus_modal", {"code": "BONUS"}))
    await bot.handle_bonus_modal(cast(discord.Interaction, fake))
    sub.code_svc.apply_bonus_code.assert_awaited_once_with(username="alice", code="BONUS")
    with patch.object(bot.rendering, "run", new=AsyncMock(return_value=None)) as render:
        await bot.render_chart(cast(discord.Interaction, FakeInteraction()), 14)
        assert render.await_args is not None
        assert render.await_args.args[1] is sub.bandwidth_svc.get_bw_history.return_value
    sub.bandwidth_svc.get_bw_history.assert_awaited_once_with("alice", 14)


@pytest.mark.asyncio
async def test_chart_busy_gate_prevents_duplicate_render() -> None:
    bot, _, _, sub = make_bots()
    entered, release = asyncio.Event(), asyncio.Event()

    async def render(*args: object, **kwargs: object) -> None:
        del args, kwargs
        entered.set()
        await release.wait()

    with patch.object(bot.rendering, "run", new=AsyncMock(side_effect=render)):
        task = asyncio.create_task(bot.render_chart(cast(discord.Interaction, FakeInteraction()), 14))
        await asyncio.wait_for(entered.wait(), 1)
        second = FakeInteraction()
        await bot.render_chart(cast(discord.Interaction, second), 14)
        release.set()
        await task
    assert "generating" in str(second.response.messages[-1]["content"]).lower()
    assert sub.bandwidth_svc.get_bw_history.await_count == 1
