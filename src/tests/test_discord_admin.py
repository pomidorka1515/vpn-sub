from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from discord_helpers import FakeInteraction, make_bots, modal_data

import discord
from core.services.refresh import RefreshResult


@pytest.mark.asyncio
async def test_admin_whitelist_guards_service_access() -> None:
    _, bot, _, sub = make_bots(whitelist=[99])
    fake = FakeInteraction(data={"custom_id": "admin:list_users"})
    await bot.dispatch_component(cast(discord.Interaction, fake))
    sub.user_svc.list_users.assert_not_awaited()
    assert "Нет доступа" in str(fake.response.messages[-1]["content"])


@pytest.mark.asyncio
async def test_admin_list_and_edit_menu_routing() -> None:
    _, bot, _, sub = make_bots()
    fake = FakeInteraction(data={"custom_id": "admin:list_users"})
    await bot.dispatch_component(cast(discord.Interaction, fake))
    sub.user_svc.list_users.assert_awaited_once()
    assert "bob" in str(fake.response.messages[-1]["content"])
    edit = FakeInteraction(data={"custom_id": "admin:edit_user:alice"})
    await bot.dispatch_component(cast(discord.Interaction, edit))
    assert bot._pending_edits[7] == {"username": "alice"}
    for custom_id in (
        "admin:edit:limit",
        "admin:edit:wl_limit",
        "admin:edit:time",
        "admin:edit:name",
    ):
        modal = FakeInteraction(data={"custom_id": custom_id})
        await bot.dispatch_component(cast(discord.Interaction, modal))
        assert modal.response.modals
        assert not modal.response.deferred
    sub.business_svc.get_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_user_calls_business_service() -> None:
    _, bot, _, sub = make_bots()
    fake = FakeInteraction(
        data=modal_data(
            "admin:modal:add_user",
            {
                "username": "alice",
                "displayname": "Ada",
                "limit": "10",
                "days": "0",
            },
        ),
    )
    await bot.dispatch_modal(cast(discord.Interaction, fake))
    sub.business_svc.add_new_user.assert_awaited_once_with(
        username="alice", displayname="Ada", limit=10, timee=0,
    )


@pytest.mark.asyncio
async def test_permanent_code_uses_shared_service() -> None:
    _, bot, _, sub = make_bots()
    bot._pending_codes[7] = {
        "name": "WELCOME",
        "type": "register",
        "days": 30,
        "gb": 10,
        "wl_gb": 5,
    }
    fake = FakeInteraction(data={"custom_id": "admin:code_perma:yes"})
    await bot.dispatch_component(cast(discord.Interaction, fake))
    sub.code_svc.add_code.assert_awaited_once_with(
        "WELCOME", "register", permanent=True, days=30, gb=10, wl_gb=5, uses=-1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "needle"),
    [
        (RefreshResult(["alice"], "bob", 1, 3), "прервано"),
        (RefreshResult(["alice"], None, 1, 2), "ошибками"),
        (RefreshResult([], None, 2, 2), "успешно"),
    ],
)
async def test_refresh_reports_shared_partial_failure_result(
    result: RefreshResult, needle: str,
) -> None:
    _, bot, _, sub = make_bots()
    sub.refresh_svc.refresh.return_value = result
    fake = FakeInteraction(data={"custom_id": "admin:refresh_all"})
    await bot.dispatch_component(cast(discord.Interaction, fake))
    assert needle in str(fake.response.messages[-1]["content"])
    sub.refresh_svc.refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_leaderboard_uses_service_mapping_and_discord_text_limit() -> None:
    _, bot, _, sub = make_bots()
    sub.leaderboard_svc.leaderboard.return_value = {
        f"user-{index:03d}": 2_000_000_000 for index in range(40)
    }
    bot._pending_leaderboard[7] = {"type": "total", "order": "desc"}
    fake = FakeInteraction(data=modal_data("admin:modal:lb_window", {"window": "0"}))
    with patch.object(bot.rendering, "run", new=AsyncMock(return_value=None)):
        await bot.dispatch_modal(cast(discord.Interaction, fake))
    sub.leaderboard_svc.leaderboard.assert_awaited_once_with(
        "total", top_n=0, use_displaynames=True, flip=False,
    )
    text = str(fake.response.messages[-1]["content"])
    assert "user-000" in text
    assert 1024 < len(text.encode()) <= 2000
