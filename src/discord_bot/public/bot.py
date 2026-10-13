from __future__ import annotations

from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

from loggers import Logger

from .account import PublicAccountMixin
from .common import PublicCommonMixin
from .login import PublicLoginMixin
from .routing import PublicRoutingMixin
from .settings import PublicSettingsMixin
from .subscription import PublicSubscriptionMixin
from .traffic import PublicTrafficMixin

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

    import discord
    from app_runtime.offload import BoundedOffload
    from config import DiscordConfig, DiscordLangConfig
    from core.common import Snapshot
    from discord import Client, Interaction, app_commands
    from discord_bot.sessions import SessionStore
    from discord_bot.webapi import WebApiClient

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
        cfg: Snapshot[DiscordConfig],
        lang_cfg: Snapshot[DiscordLangConfig],
        http: WebApiClient,
        sessions: SessionStore,
        *,
        client: discord.Client,
        tree: app_commands.CommandTree,
        rendering: BoundedOffload,
    ) -> None:
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg = cfg
            self.lang_cfg = lang_cfg
            self.http = http
            self.rendering = rendering
            self.sessions = sessions
            self.bot = client
            self.tree = tree
            language = lang_cfg.view()
            self.TEXTS = language["public"]
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
            command_handler: Callable[[discord.Interaction[Client]], Coro],
        ) -> Callable[[discord.Interaction[Client]], Coro]:
            async def wrapped(interaction: discord.Interaction) -> None:
                from discord_bot.host import SharedDiscordClient

                async def invoke() -> None:
                    if self.command_requires_auth(command_name) and not self.is_logged_in(interaction.user.id):
                        await self._reply_key(interaction, "not_logged_in")
                        return
                    await command_handler(interaction)
                if isinstance(self.bot, SharedDiscordClient):
                    await self.bot.process(str(interaction.user.id), invoke)

            wrapped.__name__ = command_name
            wrapped.__qualname__ = command_name
            return wrapped

        for name, handler, description in commands:
            self.tree.command(name=name, description=description)(bind(name, handler))

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return
