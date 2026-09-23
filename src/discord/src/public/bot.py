from __future__ import annotations

import asyncio
from typing import Any, cast

import discord
from discord import app_commands

from config import ConfigLike
from loggers import Logger

from webapi import WebApiClient
from sessions import SessionStore

from .account import PublicAccountMixin
from .common import PublicCommonMixin
from .login import PublicLoginMixin
from .routing import PublicRoutingMixin
from .settings import PublicSettingsMixin
from .subscription import PublicSubscriptionMixin
from .traffic import PublicTrafficMixin

__all__ = ["PublicBot"]


class _PublicClient(discord.Client):
    """Client that syncs slash commands once in setup_hook, not on every ready."""

    def __init__(self, owner: PublicBot, *, intents: discord.Intents) -> None:
        super().__init__(intents=intents)
        self._owner = owner

    async def setup_hook(self) -> None:
        try:
            await self._owner.tree.sync()
        except Exception:
            self._owner.log.error("failed to sync slash commands", exc_info=True)


class PublicBot(
    PublicCommonMixin,
    PublicLoginMixin,
    PublicSettingsMixin,
    PublicSubscriptionMixin,
    PublicAccountMixin,
    PublicTrafficMixin,
    PublicRoutingMixin,
):
    def __init__(
        self,
        cfg: ConfigLike,
        lang_cfg: ConfigLike,
        http: WebApiClient,
        sessions: SessionStore,
    ) -> None:
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg = cfg
            self.lang_cfg = lang_cfg
            self.http = http
            self.sessions = sessions
            token = self.cfg["public"]["token"]
            if not isinstance(token, str) or not token:
                raise RuntimeError("public discord bot token not found in discord.json")
            self._token = token
            self.TEXTS = cast(dict[str, dict[str, str]], lang_cfg["public"])
            intents = discord.Intents.default()
            self.bot = _PublicClient(self, intents=intents)
            self.tree = app_commands.CommandTree(self.bot)
            self._chart_locks: dict[int, asyncio.Lock] = {}
            self._wire_events()
            self._wire_commands()

    def _wire_events(self) -> None:
        @self.bot.event
        async def on_ready() -> None:
            self.log.info("public discord bot is ready")

        @self.bot.event
        async def on_interaction(interaction: discord.Interaction) -> None:
            if interaction.type is discord.InteractionType.component:
                await self.dispatch_component(interaction)
            elif interaction.type is discord.InteractionType.modal_submit:
                await self.dispatch_modal(interaction)

    def _wire_commands(self) -> None:
        commands: tuple[tuple[str, Any, str], ...] = (
            ("start", self.cmd_start, "Open the language gate or main menu"),
            ("menu", self.cmd_start, "Open the language gate or main menu"),
            ("login", self.cmd_login, "Log in with username and password"),
            ("register", self.cmd_register, "Register with an invite code"),
            ("info", self.cmd_info, "Show your subscription info"),
            ("sub", self.cmd_sub, "Send your subscription link and QR"),
            ("bonus", self.cmd_bonus, "Apply a bonus code"),
            ("chart", self.cmd_chart, "Show bandwidth history"),
            ("settings", self.cmd_settings, "Change name, fingerprint, login, or password"),
            ("help", self.cmd_help, "Show VPN profile descriptions"),
            ("support", self.cmd_support, "Show support contact"),
            ("logout", self.cmd_logout, "Log out of the website and this bot"),
            ("reset", self.cmd_reset, "Reset your subscription link"),
            ("delete", self.cmd_delete, "Delete your account"),
        )
        for name, handler, description in commands:
            self.tree.command(name=name, description=description)(handler)

    async def start(self) -> None:
        await self.bot.login(self._token)
        self._runner = asyncio.create_task(self.bot.connect(reconnect=True), name="public-discord")

    async def stop(self) -> None:
        runner = getattr(self, "_runner", None)
        if self.bot.is_closed() is False:
            await self.bot.close()
        if runner is not None:
            try:
                await runner
            except Exception:
                self.log.error("public discord runner failed", exc_info=True)
