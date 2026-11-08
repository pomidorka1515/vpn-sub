from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import discord
import pytest

from errors import CommittedAuditError
from tests.support.discord import FakeInteraction, make_bots, modal_data

if TYPE_CHECKING:
    from bots.discord.admin import DiscordAdminBot


async def component(bot: DiscordAdminBot, custom_id: str) -> FakeInteraction:
    interaction = FakeInteraction(data={"custom_id": custom_id})
    await bot.dispatch_component(cast(discord.Interaction, interaction))
    return interaction


async def submit(bot: DiscordAdminBot, custom_id: str, fields: dict[str, str]) -> FakeInteraction:
    generation = custom_id.rsplit(":", 1)[1]
    interaction = FakeInteraction(
        data=modal_data(custom_id, {f"{key}:{generation}": value for key, value in fields.items()})
    )
    await bot.dispatch_modal(cast(discord.Interaction, interaction))
    return interaction


def modal_id(interaction: FakeInteraction) -> str:
    modal = cast(discord.ui.Modal, interaction.response.modals[-1])
    generation = modal.custom_id.rsplit(":", 1)[1]
    assert all(
        isinstance(item, discord.ui.TextInput) and item.custom_id.endswith(f":{generation}")
        for item in modal.children
    )
    return modal.custom_id


def button_ids(interaction: FakeInteraction) -> list[str]:
    view = cast(discord.ui.View, interaction.response.messages[-1]["view"])
    return [
        item.custom_id
        for item in view.children
        if isinstance(item, discord.ui.Button) and item.custom_id is not None
    ]


async def start(bot: DiscordAdminBot) -> str:
    interaction = await component(bot, "admin:add_code")
    custom_id = modal_id(interaction)
    assert custom_id.startswith("admin:modal:add_code:")
    return custom_id.rsplit(":", 1)[1]


async def name(bot: DiscordAdminBot, generation: str, value: str = "WELCOME") -> FakeInteraction:
    return await submit(bot, f"admin:modal:add_code:{generation}", {"code": value})


async def days(bot: DiscordAdminBot, generation: str) -> FakeInteraction:
    return await submit(
        bot, f"admin:modal:code_days:{generation}", {"days": "30", "gb": "10", "wl_gb": "5"}
    )


async def to_perma(bot: DiscordAdminBot, generation: str) -> None:
    await name(bot, generation)
    interaction = await component(bot, f"admin:codetype:register:{generation}")
    assert modal_id(interaction) == f"admin:modal:code_days:{generation}"
    await days(bot, generation)


@pytest.mark.asyncio
async def test_overlapping_workflows_reject_all_stale_interactions() -> None:
    _, bot, _, sub = make_bots()
    old = await start(bot)
    await to_perma(bot, old)
    uses = await component(bot, f"admin:code_perma:no:{old}")
    assert modal_id(uses) == f"admin:modal:code_uses:{old}"
    new = await start(bot)
    assert new != old
    current = bot._pending_codes[7]
    stale = [
        await name(bot, old, "OLD"),
        await component(bot, f"admin:codetype:bonus:{old}"),
        await days(bot, old),
        await component(bot, f"admin:code_perma:yes:{old}"),
        await submit(bot, f"admin:modal:code_uses:{old}", {"uses": "3"}),
        await component(bot, f"admin:code_cancel:perma:{old}"),
        await component(bot, f"admin:code_retry:uses:{old}"),
    ]
    assert all("истекла" in str(item.response.messages[-1]["content"]) for item in stale)
    assert bot._pending_codes[7] is current
    sub.code_svc.add_code.assert_not_awaited()
    await to_perma(bot, new)
    await component(bot, f"admin:code_perma:yes:{new}")
    sub.code_svc.add_code.assert_awaited_once_with(
        "WELCOME", "register", permanent=True, days=30, gb=10, wl_gb=5, uses=-1
    )


@pytest.mark.asyncio
async def test_stages_duplicates_and_cancel() -> None:
    _, bot, _, sub = make_bots()
    generation = await start(bot)
    assert "истекла" in str(
        (await component(bot, f"admin:code_perma:yes:{generation}")).response.messages[-1][
            "content"
        ]
    )
    choice = await name(bot, generation)
    assert set(button_ids(choice)) == {
        f"admin:codetype:register:{generation}",
        f"admin:codetype:bonus:{generation}",
        f"admin:code_cancel:type:{generation}",
    }
    await component(bot, f"admin:codetype:bonus:{generation}")
    duplicate = await component(bot, f"admin:codetype:register:{generation}")
    assert "истекла" in str(duplicate.response.messages[-1]["content"])
    selection = await days(bot, generation)
    assert set(button_ids(selection)) == {
        f"admin:code_perma:yes:{generation}",
        f"admin:code_perma:no:{generation}",
        f"admin:code_cancel:perma:{generation}",
    }
    await component(bot, f"admin:code_perma:no:{generation}")
    stale = await component(bot, f"admin:code_perma:yes:{generation}")
    assert "истекла" in str(stale.response.messages[-1]["content"])
    stale_cancel = await component(bot, f"admin:code_cancel:perma:{generation}")
    assert "истекла" in str(stale_cancel.response.messages[-1]["content"])
    await component(bot, f"admin:code_cancel:uses:{generation}")
    assert 7 not in bot._pending_codes
    await submit(bot, f"admin:modal:code_uses:{generation}", {"uses": "2"})
    sub.code_svc.add_code.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_and_malformed_creation_ids_cannot_change_workflow() -> None:
    _, bot, _, sub = make_bots()
    generation = await start(bot)
    current = bot._pending_codes[7]
    for custom_id in ("admin:modal:add_code", "admin:modal:code_days", "admin:modal:code_uses"):
        await submit(
            bot, custom_id, {"code": "OLD", "days": "1", "gb": "1", "wl_gb": "1", "uses": "1"}
        )
    for custom_id in (
        "admin:codetype:register",
        "admin:code_perma:yes",
        f"admin:codetype:other:{generation}",
        f"admin:code_perma:yes:{generation}:extra",
    ):
        await component(bot, custom_id)
    assert bot._pending_codes[7] is current
    assert current["stage"] == "name"
    sub.code_svc.add_code.assert_not_awaited()


@pytest.mark.asyncio
async def test_validation_retries_and_valid_nonpermanent_flow() -> None:
    _, bot, _, sub = make_bots()
    generation = await start(bot)
    bad_name = await name(bot, generation, " ")
    assert f"admin:code_retry:name:{generation}" in button_ids(bad_name)
    assert (
        modal_id(await component(bot, f"admin:code_retry:name:{generation}"))
        == f"admin:modal:add_code:{generation}"
    )
    await name(bot, generation)
    await component(bot, f"admin:codetype:bonus:{generation}")
    stale_retry = await component(bot, f"admin:code_retry:name:{generation}")
    assert "истекла" in str(stale_retry.response.messages[-1]["content"])
    for value in ("oops", "-1"):
        invalid = await submit(
            bot, f"admin:modal:code_days:{generation}", {"days": value, "gb": "0", "wl_gb": "0"}
        )
        assert f"admin:code_retry:days:{generation}" in button_ids(invalid)
        assert (
            modal_id(await component(bot, f"admin:code_retry:days:{generation}"))
            == f"admin:modal:code_days:{generation}"
        )
    await days(bot, generation)
    await component(bot, f"admin:code_perma:no:{generation}")
    invalid = await submit(bot, f"admin:modal:code_uses:{generation}", {"uses": "0"})
    assert f"admin:code_retry:uses:{generation}" in button_ids(invalid)
    assert (
        modal_id(await component(bot, f"admin:code_retry:uses:{generation}"))
        == f"admin:modal:code_uses:{generation}"
    )
    await submit(bot, f"admin:modal:code_uses:{generation}", {"uses": "2"})
    await submit(bot, f"admin:modal:code_uses:{generation}", {"uses": "2"})
    sub.code_svc.add_code.assert_awaited_once_with(
        "WELCOME", "bonus", permanent=False, days=30, gb=10, wl_gb=5, uses=2
    )
    assert 7 not in bot._pending_codes


@pytest.mark.asyncio
@pytest.mark.parametrize("permanent", [True, False])
async def test_service_failure_can_retry(permanent: bool) -> None:
    _, bot, _, sub = make_bots()
    generation = await start(bot)
    await to_perma(bot, generation)
    sub.code_svc.add_code.side_effect = [RuntimeError("failed"), None]
    if permanent:
        failure = await component(bot, f"admin:code_perma:yes:{generation}")
        assert f"admin:code_perma:yes:{generation}" in button_ids(failure)
        await component(bot, f"admin:code_perma:yes:{generation}")
    else:
        await component(bot, f"admin:code_perma:no:{generation}")
        failure = await submit(bot, f"admin:modal:code_uses:{generation}", {"uses": "2"})
        assert f"admin:code_retry:uses:{generation}" in button_ids(failure)
        await component(bot, f"admin:code_retry:uses:{generation}")
        await submit(bot, f"admin:modal:code_uses:{generation}", {"uses": "2"})
    assert sub.code_svc.add_code.await_count == 2
    assert 7 not in bot._pending_codes


@pytest.mark.asyncio
async def test_committed_audit_failure_is_not_retried() -> None:
    _, bot, _, sub = make_bots()
    generation = await start(bot)
    await to_perma(bot, generation)
    sub.code_svc.add_code.side_effect = CommittedAuditError()
    failure = await component(bot, f"admin:code_perma:yes:{generation}")
    assert "создан" in str(failure.response.messages[-1]["content"])
    assert 7 not in bot._pending_codes
    await component(bot, f"admin:code_perma:yes:{generation}")
    sub.code_svc.add_code.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("permanent", [False, True])
async def test_inflight_duplicate_and_new_workflow_are_isolated(
    fail: bool, permanent: bool
) -> None:
    _, bot, _, sub = make_bots()
    generation = await start(bot)
    await to_perma(bot, generation)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def add(*args: object, **kwargs: object) -> None:
        entered.set()
        await release.wait()
        if fail:
            raise RuntimeError("failed")

    sub.code_svc.add_code.side_effect = add
    if not permanent:
        await component(bot, f"admin:code_perma:no:{generation}")

    async def action() -> FakeInteraction:
        if permanent:
            return await component(bot, f"admin:code_perma:yes:{generation}")
        return await submit(bot, f"admin:modal:code_uses:{generation}", {"uses": "2"})

    task = asyncio.create_task(action())
    await entered.wait()
    duplicate = await action()
    assert "истекла" in str(duplicate.response.messages[-1]["content"])
    newer = await start(bot)
    await name(bot, newer, "NEW")
    current = bot._pending_codes[7]
    release.set()
    await task
    assert bot._pending_codes[7] is current
    assert current["name"] == "NEW"
    sub.code_svc.add_code.assert_awaited_once()
