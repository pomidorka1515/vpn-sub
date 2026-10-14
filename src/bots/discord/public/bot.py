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
    from app_runtime.rate_limit import AsyncRateLimiter
    from bots.discord.sessions import SessionStore
    from config import AppConfig, DiscordLangConfig
    from core import Subscription
    from core.common import Snapshot
    from discord import Client, Interaction, app_commands

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
        cfg: Snapshot[AppConfig],
        lang_cfg: Snapshot[DiscordLangConfig],
        sub: Subscription,
        sessions: SessionStore,
        *,
        client: discord.Client,
        tree: app_commands.CommandTree,
        rendering: BoundedOffload,
        limiter: AsyncRateLimiter,
    ) -> None:
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg = cfg
            self.lang_cfg = lang_cfg
            self.sub = sub
            self.limiter = limiter
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
                from bots.discord.host import SharedDiscordClient

                async def invoke() -> None:
                    if self.command_requires_auth(command_name) and not await self.require_login(
                        interaction,
                    ):
                        return
                    await command_handler(interaction)

                if isinstance(self.bot, SharedDiscordClient):
                    await self.bot.interact(interaction, invoke, operation=command_name)

            wrapped.__name__ = command_name
            wrapped.__qualname__ = command_name
            return wrapped

        for name, handler, description in commands:
            self.tree.command(name=name, description=description)(bind(name, handler))
