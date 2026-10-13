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
    from config import DiscordConfig, DiscordLangConfig
    from core.common import Snapshot
    from discord import app_commands
    from discord_bot.adminapi import AdminApiClient

__all__ = ["AdminBot"]


class AdminBot(
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
        cfg: Snapshot[DiscordConfig],
        lang_cfg: Snapshot[DiscordLangConfig],
        http: AdminApiClient,
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
            self.bot = client
            self.tree = tree
            conf = cfg.view()
            whitelist = conf["private"]["whitelist"]
            self.admin_uids = [int(item) for item in whitelist]
            self._pending_codes: dict[int, dict[str, str | int | bool]] = {}
            self._pending_edits: dict[int, dict[str, str]] = {}
            self._pagination_state: dict[int, dict[str, int]] = {}
            self._pending_leaderboard: dict[int, dict[str, str | int]] = {}
            self._wire_commands()

    def _wire_commands(self) -> None:
        async def wrapped(interaction: discord.Interaction) -> None:
            from discord_bot.host import SharedDiscordClient

            if isinstance(self.bot, SharedDiscordClient):
                await self.bot.process(str(interaction.user.id), lambda: self.cmd_admin(interaction))

        wrapped.__name__ = "admin"
        wrapped.__qualname__ = "admin"
        self.tree.command(name="admin", description="Open the VPN admin panel")(wrapped)

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return
