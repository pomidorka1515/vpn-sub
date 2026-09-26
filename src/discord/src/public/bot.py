from __future__ import annotations

import asyncio
from typing import Any, cast
from collections.abc import Callable, Coroutine
import discord
from discord import app_commands
from discord import Client, Interaction

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

type Coro[T = None] = Coroutine[Any, Any, T]

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
        *,
        client: discord.Client,
        tree: app_commands.CommandTree,
    ) -> None:
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg = cfg
            self.lang_cfg = lang_cfg
            self.http = http
            self.sessions = sessions
            self.bot = client
            self.tree = tree
            self.TEXTS = cast(dict[str, dict[str, str]], lang_cfg["public"])
            self._chart_locks: dict[int, asyncio.Lock] = {}
            self._chart_busy: set[int] = set()
            self._wire_commands()

    def _wire_commands(self) -> None:
        commands: tuple[tuple[str, Callable[[Interaction[Client]], Coro], str], ...] = (
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
        def bind(
            command_name: str, 
            command_handler: Callable[[discord.Interaction[Client]], Coro]
        ) -> Callable[[discord.Interaction[Client]], Coro]:
            async def wrapped(interaction: discord.Interaction) -> None:
                if self.command_requires_auth(command_name) and not self.is_logged_in(interaction.user.id):
                    await self._reply_key(interaction, "not_logged_in")
                    return
                await command_handler(interaction)
            wrapped.__name__ = command_name
            wrapped.__qualname__ = command_name
            return wrapped

        for name, handler, description in commands:
            self.tree.command(name=name, description=description)(bind(name, handler))

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return
