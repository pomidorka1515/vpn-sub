from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import discord
import pytest

from errors import CommittedAuditError
from tests.support.discord import FakeInteraction, make_bots, modal_data

if TYPE_CHECKING:
    from bots.discord.admin import DiscordAdminBot


FIELDS = {
    "limit": ("limit", "10", "limit", 10),
    "wl_limit": ("wl_limit", "5", "wl_limit", 5),
    "time": ("days", "0", "timee", 0),
    "name": ("name", "Ada", "displayname", "Ada"),
    "fp": ("fingerprint", "chrome", "fingerprint", "chrome"),
}


def interaction_for(field: str, generation: str, value: str | None = None) -> FakeInteraction:
    key, default, _, _ = FIELDS[field]
    if field == "fp":
        return FakeInteraction(data={"custom_id": f"admin:fp_save:{value or default}:{generation}"})
    return FakeInteraction(
        data=modal_data(
            f"admin:modal:edit_{field}:{generation}", {key: default if value is None else value}
        )
    )


async def dispatch(bot: DiscordAdminBot, field: str, interaction: FakeInteraction) -> None:
    if field == "fp":
        await bot.dispatch_component(cast(discord.Interaction, interaction))
    else:
        await bot.dispatch_modal(cast(discord.Interaction, interaction))


@pytest.mark.asyncio
@pytest.mark.parametrize("field", FIELDS)
async def test_edit_defers_before_blocked_service_and_follows_up(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bot, _, sub = make_bots()
    bot._pending_edits[7] = {"username": "alice", "generation": "current"}
    interaction = interaction_for(field, "current")
    followup = AsyncMock(wraps=interaction.followup.send)
    monkeypatch.setattr(interaction.followup, "send", followup)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def update(**kwargs: object) -> None:
        assert interaction.response.deferred
        assert not interaction.response.messages
        entered.set()
        await release.wait()

    sub.business_svc.update_params.side_effect = update
    task = asyncio.create_task(dispatch(bot, field, interaction))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert not task.done()
    finally:
        release.set()
        await task
    _, _, key, value = FIELDS[field]
    sub.business_svc.update_params.assert_awaited_once_with(username="alice", **{key: value})
    followup.assert_awaited_once()
    assert "✅" in str(interaction.response.messages[-1]["content"])
    assert 7 not in bot._pending_edits


@pytest.mark.asyncio
@pytest.mark.parametrize("field", FIELDS)
async def test_stale_edit_is_rejected_without_deferral(field: str) -> None:
    _, bot, _, sub = make_bots()
    pending = {"username": "alice", "generation": "current"}
    bot._pending_edits[7] = pending
    interaction = interaction_for(field, "stale")
    await dispatch(bot, field, interaction)
    assert not interaction.response.deferred
    assert "истекла" in str(interaction.response.messages[-1]["content"])
    assert bot._pending_edits[7] is pending
    sub.business_svc.update_params.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [("limit", "-1"), ("wl_limit", "oops"), ("time", "-1"), ("name", "x" * 17), ("fp", "invalid")],
)
async def test_invalid_edit_is_rejected_without_deferral(field: str, value: str) -> None:
    _, bot, _, sub = make_bots()
    pending = {"username": "alice", "generation": "current"}
    bot._pending_edits[7] = pending
    interaction = interaction_for(field, "current", value)
    await dispatch(bot, field, interaction)
    assert not interaction.response.deferred
    assert "❌" in str(interaction.response.messages[-1]["content"])
    assert bot._pending_edits[7] is pending
    sub.business_svc.update_params.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("committed", [False, True])
async def test_edit_failure_reports_outcome_on_followup(
    field: str, committed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bot, _, sub = make_bots()
    pending = {"username": "alice", "generation": "current"}
    bot._pending_edits[7] = pending
    interaction = interaction_for(field, "current")
    followup = AsyncMock(wraps=interaction.followup.send)
    monkeypatch.setattr(interaction.followup, "send", followup)
    sub.business_svc.update_params.side_effect = (
        CommittedAuditError() if committed else RuntimeError("service failure")
    )
    await dispatch(bot, field, interaction)
    assert interaction.response.deferred
    followup.assert_awaited_once()
    message = str(interaction.response.messages[-1]["content"])
    if committed:
        assert "сохранено" in message
        assert "аудита" in message
        assert "Не повторяйте" in message
        assert 7 not in bot._pending_edits
        await dispatch(bot, field, interaction_for(field, "current"))
        sub.business_svc.update_params.assert_awaited_once()
    else:
        assert "Ошибка" in message
        assert "Проверьте" in message
        assert bot._pending_edits[7] is pending


@pytest.mark.asyncio
@pytest.mark.parametrize("field", FIELDS)
async def test_failed_defer_prevents_mutation(field: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _, bot, _, sub = make_bots()
    pending = {"username": "alice", "generation": "current"}
    bot._pending_edits[7] = pending
    interaction = interaction_for(field, "current")
    monkeypatch.setattr(
        interaction.response, "defer", AsyncMock(side_effect=RuntimeError("discord unavailable"))
    )
    await dispatch(bot, field, interaction)
    sub.business_svc.update_params.assert_not_awaited()
    assert bot._pending_edits[7] is pending


@pytest.mark.asyncio
@pytest.mark.parametrize("field", FIELDS)
async def test_edit_rechecks_generation_after_defer(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bot, _, sub = make_bots()
    bot._pending_edits[7] = {"username": "alice", "generation": "current"}
    interaction = interaction_for(field, "current")
    original_defer = interaction.response.defer
    new_pending = {"username": "bob", "generation": "new"}

    async def defer(**kwargs: object) -> None:
        bot._pending_edits[7] = new_pending
        await original_defer(**kwargs)

    monkeypatch.setattr(interaction.response, "defer", defer)
    await dispatch(bot, field, interaction)
    assert interaction.response.deferred
    assert "истекла" in str(interaction.response.messages[-1]["content"])
    assert bot._pending_edits[7] is new_pending
    sub.business_svc.update_params.assert_not_awaited()


@pytest.mark.asyncio
async def test_expiry_modal_explains_replacement() -> None:
    _, bot, _, _ = make_bots()
    bot._pending_edits[7] = {"username": "alice", "generation": "current"}
    interaction = FakeInteraction()
    await bot._cb_edit_time(cast(discord.Interaction, interaction), "alice")
    modal = cast(discord.ui.Modal, interaction.response.modals[0])
    assert modal.title == "Замена срока"
    assert modal.custom_id == "admin:modal:edit_time:current"
    field = cast(discord.ui.TextInput[discord.ui.Modal], modal.children[0])
    assert field.to_component_dict()["label"] == "Дней от текущего момента (0 = безлимит)"
    assert field.placeholder == "Текущий срок будет заменён"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original_expiry", [1_800_000_000, 1_600_000_000, 0], ids=["future", "expired", "unlimited"]
)
async def test_expiry_days_replace_existing_timestamp_from_now(
    original_expiry: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bot, _, sub = make_bots()
    bot._pending_edits[7] = {"username": "alice", "generation": "current"}
    monkeypatch.setattr(
        "bots.discord.admin.users.time", SimpleNamespace(time=lambda: 1_700_000_000)
    )
    state = {"expiry": original_expiry}

    async def update(*, username: str, timee: int) -> None:
        assert username == "alice"
        state["expiry"] = timee

    sub.business_svc.update_params.side_effect = update
    interaction = interaction_for("time", "current", "2")
    await dispatch(bot, "time", interaction)
    sub.business_svc.update_params.assert_awaited_once_with(
        username="alice", timee=1_700_000_000 + 2 * 86400
    )
    assert state["expiry"] == 1_700_000_000 + 2 * 86400
    message = str(interaction.response.messages[-1]["content"])
    assert "Срок заменён" in message
    assert "`2` дней от текущего момента" in message
    assert "16.11.23 22:13 (UTC)" in message
    assert "продл" not in message


@pytest.mark.asyncio
async def test_zero_expiry_replaces_existing_timestamp_with_unlimited() -> None:
    _, bot, _, sub = make_bots()
    bot._pending_edits[7] = {"username": "alice", "generation": "current"}
    interaction = interaction_for("time", "current", "0")
    await dispatch(bot, "time", interaction)
    sub.business_svc.update_params.assert_awaited_once_with(username="alice", timee=0)
    assert "Срок заменён на безлимит" in str(interaction.response.messages[-1]["content"])
