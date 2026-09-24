from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from loggers import Logger

if TYPE_CHECKING:
    from admin.bot import AdminBot
    from public.bot import PublicBot

__all__ = ["SharedDiscordClient"]

log = Logger("discord.host")


class SharedDiscordClient(discord.Client):
    """One Discord client that syncs the shared tree and fans out interactions."""

    def __init__(
        self,
        *,
        intents: discord.Intents,
        tree: app_commands.CommandTree | None = None,
    ) -> None:
        super().__init__(intents=intents)
        self.tree = tree or app_commands.CommandTree(self)
        self._public: PublicBot | None = None
        self._admin: AdminBot | None = None
        self._wire_events()

    def attach(self, public: PublicBot, admin: AdminBot) -> None:
        self._public = public
        self._admin = admin

    def _wire_events(self) -> None:
        @self.event
        async def on_ready() -> None:
            log.info("discord bot is ready")

        @self.event
        async def on_interaction(interaction: discord.Interaction) -> None:
            custom_id = _custom_id(interaction)
            public = self._public
            admin = self._admin
            if public is None or admin is None:
                return
            if interaction.type is discord.InteractionType.component:
                await _dispatch(custom_id, admin.dispatch_component, public.dispatch_component, interaction)
            elif interaction.type is discord.InteractionType.modal_submit:
                await _dispatch(custom_id, admin.dispatch_modal, public.dispatch_modal, interaction)

    async def setup_hook(self) -> None:
        try:
            await self.tree.sync()
        except Exception:
            log.error("failed to sync slash commands", exc_info=True)


def _custom_id(interaction: discord.Interaction) -> str:
    data = interaction.data
    if not isinstance(data, dict):
        return ""
    custom_id = data.get("custom_id")
    return custom_id if isinstance(custom_id, str) else ""


async def _dispatch(
    custom_id: str,
    admin: Callable[[discord.Interaction], Awaitable[None]],
    public: Callable[[discord.Interaction], Awaitable[None]],
    interaction: discord.Interaction,
) -> None:
    if custom_id.startswith("admin"):
        await admin(interaction)
        return
    await public(interaction)
