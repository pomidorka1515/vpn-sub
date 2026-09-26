from __future__ import annotations


import discord
from discord import app_commands

from config import ConfigLike
from loggers import Logger

from adminapi import AdminApiClient

from .codes import AdminCodesMixin
from .common import AdminCommonMixin
from .leaderboard import AdminLeaderboardMixin
from .panels import AdminPanelsMixin
from .routing import AdminRoutingMixin
from .traffic import AdminTrafficMixin
from .users import AdminUsersMixin

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
        cfg: ConfigLike,
        lang_cfg: ConfigLike,
        http: AdminApiClient,
        *,
        client: discord.Client,
        tree: app_commands.CommandTree,
    ) -> None:
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg = cfg
            self.lang_cfg = lang_cfg
            self.http = http
            self.bot = client
            self.tree = tree
            whitelist: list[int] = cfg["private"]["whitelist"]
            self.admin_uids = [int(item) for item in whitelist]
            self._pending_codes: dict[int, dict[str, str | int | bool]] = {}
            self._pending_edits: dict[int, dict[str, str]] = {}
            self._pagination_state: dict[int, dict[str, int]] = {}
            self._pending_leaderboard: dict[int, dict[str, str | int]] = {}
            self._wire_commands()

    def _wire_commands(self) -> None:
        async def wrapped(interaction: discord.Interaction) -> None:
            await self.cmd_admin(interaction)

        wrapped.__name__ = "admin"
        wrapped.__qualname__ = "admin"
        self.tree.command(name="admin", description="Open the VPN admin panel")(wrapped)

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return
