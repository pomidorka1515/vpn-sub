from __future__ import annotations

from typing import cast

import discord
import pytest

from tests.support.discord import FakeInteraction, make_bots, modal_data


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["used", "wl_used"])
async def test_used_editor_routes_and_validates(field: str) -> None:
    _, bot, _, sub = make_bots()
    bot._pending_edits[7] = {"username": "alice", "generation": "current"}
    component = FakeInteraction(data={"custom_id": f"admin:edit:{field}:current"})
    await bot.dispatch_component(cast(discord.Interaction, component))
    modal = cast(discord.ui.Modal, component.response.modals[0])
    assert modal.custom_id == f"admin:modal:edit_{field}:current"

    for generation, value in (("stale", "1"), ("current", "-1"), ("current", str(2**63))):
        interaction = FakeInteraction(
            data=modal_data(f"admin:modal:edit_{field}:{generation}", {field: value})
        )
        await bot.dispatch_modal(cast(discord.Interaction, interaction))
        sub.business_svc.edit_user.assert_not_awaited()

    interaction = FakeInteraction(
        data=modal_data(f"admin:modal:edit_{field}:current", {field: str(2**63 - 1)})
    )
    await bot.dispatch_modal(cast(discord.Interaction, interaction))
    assert interaction.response.deferred
    sub.business_svc.edit_user.assert_awaited_once_with(username="alice", **{field: 2**63 - 1})
    assert 7 not in bot._pending_edits
