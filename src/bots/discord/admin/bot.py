from __future__ import annotations

from typing import TYPE_CHECKING

from loggers import Logger

from .codes import AdminCodesMixin
from .common import AdminCommonMixin
from .leaderboard import AdminLeaderboardMixin
from .panels import AdminPanelsMixin
from .routing import AdminRoutingMixin
from .traffic import AdminTrafficMixin
from .users import AdminUsersMixin

if TYPE_CHECKING:
    import discord
    from app_runtime.offload import BoundedOffload
    from app_runtime.rate_limit import AsyncRateLimiter
    from config import AppConfig, DiscordLangConfig
    from core import Subscription
    from core.common import Snapshot
    from discord import app_commands

__all__ = ["DiscordAdminBot"]


class DiscordAdminBot(
    AdminCommonMixin,
    AdminUsersMixin,
    AdminCodesMixin,
    AdminPanelsMixin,
    AdminTrafficMixin,
    AdminLeaderboardMixin,
    AdminRoutingMixin,
):
    def __init__(
        self,
        cfg: Snapshot[AppConfig],
        lang_cfg: Snapshot[DiscordLangConfig],
        sub: Subscription,
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
            self.bot = client
            self.tree = tree
            conf = cfg.view()
            section = conf.get("discord")
            if section is None:
                raise RuntimeError("Discord configuration missing")
            whitelist = section["whitelist"]
            self.admin_uids = [int(item) for item in whitelist]
            self._pending_codes: dict[int, dict[str, str | int | bool]] = {}
            self._pending_edits: dict[int, dict[str, str]] = {}
            self._pagination_state: dict[int, dict[str, int]] = {}
            self._pending_leaderboard: dict[int, dict[str, str | int]] = {}
            self._wire_commands()

    def _wire_commands(self) -> None:
        async def wrapped(interaction: discord.Interaction) -> None:
            from bots.discord.host import SharedDiscordClient

            if isinstance(self.bot, SharedDiscordClient):
                await self.bot.interact(
                    interaction, lambda: self.cmd_admin(interaction), operation="admin", admin=True,
                )

        wrapped.__name__ = "admin"
        wrapped.__qualname__ = "admin"
        self.tree.command(name="admin", description="Open the VPN admin panel")(wrapped)
