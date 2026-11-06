from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

import discord
from core.services.refresh import RefreshResult
from tests.support.discord import FakeInteraction, make_bots, modal_data


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
    assert bot._pending_edits[7]["username"] == "alice"
    generation = bot._pending_edits[7]["generation"]
    for custom_id in (
        "admin:edit:limit",
        "admin:edit:wl_limit",
        "admin:edit:time",
        "admin:edit:name",
    ):
        modal = FakeInteraction(data={"custom_id": f"{custom_id}:{generation}"})
        await bot.dispatch_component(cast(discord.Interaction, modal))
        assert modal.response.modals
        assert not modal.response.deferred
    sub.business_svc.get_info.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("next_user", ["bob", "alice"])
@pytest.mark.parametrize("field", ["limit", "wl_limit", "time", "name", "fp"])
async def test_stale_edit_components_and_modals_are_rejected(
    next_user: str,
    field: str,
) -> None:
    _, bot, _, sub = make_bots()
    menu = FakeInteraction(data={"custom_id": "admin:edit_user:alice"})
    await bot.dispatch_component(cast(discord.Interaction, menu))
    generation = bot._pending_edits[7]["generation"]
    view = cast(discord.ui.View, menu.response.messages[-1]["view"])
    button_id = next(
        item.custom_id
        for item in view.children
        if isinstance(item, discord.ui.Button)
        and item.custom_id == f"admin:edit:{field}:{generation}"
    )
    assert button_id is not None
    edit = FakeInteraction(data={"custom_id": button_id})
    await bot.dispatch_component(cast(discord.Interaction, edit))
    if field == "fp":
        fingerprints = cast(discord.ui.View, edit.response.messages[-1]["view"])
        save_id = next(
            item.custom_id
            for item in fingerprints.children
            if isinstance(item, discord.ui.Button)
            and item.custom_id == f"admin:fp_save:chrome:{generation}"
        )
        assert save_id is not None
        stale = FakeInteraction(data={"custom_id": save_id})
    else:
        modal = cast(discord.ui.Modal, edit.response.modals[-1])
        assert modal.custom_id == f"admin:modal:edit_{field}:{generation}"
        stale = FakeInteraction(
            data=modal_data(
                modal.custom_id, {"limit": "10", "wl_limit": "10", "days": "10", "name": "New"}
            )
        )

    await bot._cb_edit_user_options(cast(discord.Interaction, FakeInteraction()), next_user)
    current = bot._pending_edits[7]
    assert current["generation"] != generation
    sub.business_svc.get_info.reset_mock()
    old_button = FakeInteraction(data={"custom_id": button_id})
    await bot.dispatch_component(cast(discord.Interaction, old_button))
    assert "истекла" in str(old_button.response.messages[-1]["content"])
    sub.business_svc.get_info.assert_not_awaited()
    if field == "fp":
        await bot.dispatch_component(cast(discord.Interaction, stale))
    else:
        await bot.dispatch_modal(cast(discord.Interaction, stale))
    assert "истекла" in str(stale.response.messages[-1]["content"])
    sub.business_svc.update_params.assert_not_awaited()
    assert bot._pending_edits[7] is current

    if field == "fp":
        active = FakeInteraction(
            data={"custom_id": f"admin:fp_save:chrome:{current['generation']}"}
        )
        await bot.dispatch_component(cast(discord.Interaction, active))
    else:
        active = FakeInteraction(
            data=modal_data(
                f"admin:modal:edit_{field}:{current['generation']}",
                {"limit": "10", "wl_limit": "10", "days": "10", "name": "New"},
            )
        )
        await bot.dispatch_modal(cast(discord.Interaction, active))
    sub.business_svc.update_params.assert_awaited_once()
    assert sub.business_svc.update_params.call_args.kwargs["username"] == next_user
    assert 7 not in bot._pending_edits


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
        username="alice",
        displayname="Ada",
        limit=10,
        timee=0,
    )


@pytest.mark.asyncio
async def test_permanent_code_uses_shared_service() -> None:
    _, bot, _, sub = make_bots()
    bot._pending_codes[7] = {
        "generation": "test",
        "stage": "perma",
        "name": "WELCOME",
        "type": "register",
        "days": 30,
        "gb": 10,
        "wl_gb": 5,
    }
    fake = FakeInteraction(data={"custom_id": "admin:code_perma:yes:test"})
    await bot.dispatch_component(cast(discord.Interaction, fake))
    sub.code_svc.add_code.assert_awaited_once_with(
        "WELCOME",
        "register",
        permanent=True,
        days=30,
        gb=10,
        wl_gb=5,
        uses=-1,
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
    result: RefreshResult,
    needle: str,
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
        "total",
        top_n=0,
        use_displaynames=True,
        flip=False,
    )
    text = str(fake.response.messages[-1]["content"])
    assert "user-000" in text
    assert 1024 < len(text.encode()) <= 2000
