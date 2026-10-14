from __future__ import annotations

import asyncio
import json
import threading
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from discord_helpers import LANG_PATH, FakeInteraction, make_bots

import discord
from app_runtime.lifecycle import Runtime
from app_runtime.offload import BoundedOffload, OffloadServices
from bots.discord.host import SharedDiscordClient, interaction_limit
from bots.discord.runtime import DiscordApplication
from config import Config, DiscordLangConfig
from db import Database
from errors import AppError, PanelUnavailableError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(path=tmp_path / "state.sqlite3", start_backup=False)
    await db.start()
    try:
        yield db
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_discord_quiesces_admitted_commands() -> None:
    client = SharedDiscordClient(intents=discord.Intents.none())
    entered, finished = asyncio.Event(), asyncio.Event()

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
    called = AsyncMock()
    await client.process("7", called)
    called.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interaction_type",
    [
        discord.InteractionType.ping,
        discord.InteractionType.application_command,
        discord.InteractionType.autocomplete,
    ],
)
async def test_host_ignores_non_component_interactions(
    interaction_type: discord.InteractionType,
) -> None:
    public, _, _, _ = make_bots()
    host = cast(SharedDiscordClient, public.bot)
    limiter = cast(MagicMock, public.limiter)
    limiter.allow.return_value = False
    fake = FakeInteraction(data={"name": "login"})
    setattr(fake, "type", interaction_type)
    try:
        await getattr(host, "on_interaction")(cast(discord.Interaction, fake))
        limiter.allow.assert_not_awaited()
        assert not fake.response.is_done()
        assert not fake.response.messages
    finally:
        await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interaction_type", "handler"),
    [
        (discord.InteractionType.component, "dispatch_component"),
        (discord.InteractionType.modal_submit, "dispatch_modal"),
    ],
)
@pytest.mark.parametrize("admin", [False, True])
async def test_host_rate_limits_and_dispatches_components_and_modals(
    interaction_type: discord.InteractionType,
    handler: str,
    admin: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public, admin_bot, _, _ = make_bots()
    host = cast(SharedDiscordClient, public.bot)
    limiter = cast(MagicMock, public.limiter)
    custom_id = "admin:refresh" if admin else "login_modal"
    fake = FakeInteraction(data={"custom_id": custom_id})
    setattr(fake, "type", interaction_type)
    dispatch = AsyncMock()
    monkeypatch.setattr(admin_bot if admin else public, handler, dispatch)
    try:
        await getattr(host, "on_interaction")(cast(discord.Interaction, fake))
        scope, limit = interaction_limit(custom_id)
        limiter.allow.assert_awaited_once_with(f"discord:{scope}", "7", limit)
        dispatch.assert_awaited_once_with(fake)
    finally:
        await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_explicit_rate_limit_is_fail_closed(failed: bool) -> None:
    public, _, _, _ = make_bots()
    host = cast(SharedDiscordClient, public.bot)
    limiter = cast(MagicMock, public.limiter)
    limiter.allow.return_value = False
    if failed:
        limiter.allow.side_effect = OSError("unavailable")
    fake = FakeInteraction()
    callback = AsyncMock()
    await host.interact(cast(discord.Interaction, fake), callback, operation="login_modal")
    callback.assert_not_awaited()
    limiter.allow.assert_awaited_once_with("discord:login", "7", 10)
    assert fake.response.messages
    await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [AppError("Invalid current password", status=401), PanelUnavailableError()],
)
async def test_domain_errors_at_interaction_boundary_preserve_valid_session(
    error: AppError,
) -> None:
    public, _, store, _ = make_bots()
    host = cast(SharedDiscordClient, public.bot)
    fake = FakeInteraction()
    await host.interact(
        cast(discord.Interaction, fake), AsyncMock(side_effect=error), operation="delete_modal",
    )
    assert store.token(7) == "token"
    assert fake.response.messages
    await host.close()


@pytest.mark.asyncio
async def test_expired_token_is_handled_at_boundary() -> None:
    public, _, store, sub = make_bots()
    sub.user_svc.auth_token_to_user.return_value = None
    host = cast(SharedDiscordClient, public.bot)
    fake = FakeInteraction()
    interaction = cast(discord.Interaction, fake)
    await host.interact(interaction, lambda: public.cmd_info(interaction), operation="info")
    assert store.token(7) is None
    assert "expired" in str(fake.response.messages[-1]["content"]).lower()
    await host.close()


@pytest.mark.asyncio
async def test_discord_startup_and_language_snapshots_without_api_routes(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    language = tmp_path / "discord.lang.jsonc"
    language.write_bytes(LANG_PATH.read_bytes())
    monkeypatch.setenv("PATH_DISCORD_LANG", str(language))
    loop_thread = threading.get_ident()
    original_view = Config[DiscordLangConfig].view

    def off_loop_view(self: Config[DiscordLangConfig]) -> DiscordLangConfig:
        assert threading.get_ident() != loop_thread
        return original_view(self)

    public, _, _, sub = make_bots()
    monkeypatch.setattr(Config, "view", off_loop_view)
    monkeypatch.setattr(SharedDiscordClient, "login", AsyncMock())
    monkeypatch.setattr(SharedDiscordClient, "connect", AsyncMock())
    files = BoundedOffload(capacity=2, name="test-files")
    rendering = BoundedOffload(capacity=2, name="test-rendering")
    runtime = DiscordApplication(
        sub,
        public.cfg,
        tmp_path,
        database,
        files=files,
        rendering=rendering,
        limiter=public.limiter,
    )
    try:
        await runtime.start()
        assert runtime.lang is not None
        feature = runtime.client._public
        assert feature is not None
        assert feature.sub is sub
        assert feature.cfg is public.cfg
        assert feature.rendering is rendering
        assert feature.limiter is public.limiter
        snapshot = runtime.lang.view()
        language.write_text("broken")
        assert runtime.lang.view() == snapshot
        snapshot["chart"]["en"]["title"] = "updated chart"
        language.write_text(json.dumps(snapshot))
        await runtime.lang.reload()
        assert feature._chart_lang("en")["title"] == "updated chart"
    finally:
        await runtime.close()
        await files.close()
        await rendering.close()


def test_component_arguments_share_rate_limit_buckets() -> None:
    assert interaction_limit("admin:info_user:alice") == interaction_limit("admin:info_user:bob")
    assert interaction_limit("admin:page:list:0") == interaction_limit("admin:page:list:1")
    assert interaction_limit("menu_info") == interaction_limit("info")


@pytest.mark.asyncio
async def test_cancelled_chart_retains_shared_rendering_capacity_until_runtime_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = BoundedOffload(capacity=1, name="test-rendering")
    runtime = Runtime(None, offloads=OffloadServices(rendering=pool))
    await runtime.start()
    public, _, _, _ = make_bots(rendering=pool)
    entered = asyncio.Event()
    release = threading.Event()
    competing = threading.Event()
    loop = asyncio.get_running_loop()

    def render(*args: object, **kwargs: object) -> None:
        del args, kwargs
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=5)

    monkeypatch.setattr("bots.discord.public.traffic.bandwidth_chart", render)
    task = asyncio.create_task(
        public.render_chart(cast(discord.Interaction, FakeInteraction()), 14),
    )
    competitor: asyncio.Task[object] | None = None
    stopping: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert pool.active_count == 1
        competitor = asyncio.create_task(pool.run(competing.set))
        await asyncio.sleep(0)
        assert not competing.is_set()
        stopping = asyncio.create_task(runtime.stop())
        await asyncio.sleep(0)
        assert not stopping.done()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        if competitor is not None:
            await asyncio.gather(competitor, return_exceptions=True)
        if stopping is not None:
            await stopping
        else:
            await runtime.stop()
    assert pool.active_count == 0
