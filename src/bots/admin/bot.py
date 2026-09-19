"""Administrator bot composition root."""

from __future__ import annotations

import threading

import telebot

from config import ConfigLike
from core import Subscription
from loggers import Logger

from .common import AdminCommonMixin
from .users import AdminUsersMixin
from .codes import AdminCodesMixin
from .panels import AdminPanelsMixin
from .traffic import AdminTrafficMixin
from .leaderboard import AdminLeaderboardMixin
__all__ = ["AdminBot"]



class AdminBot(
    AdminCommonMixin,
    AdminUsersMixin,
    AdminCodesMixin,
    AdminPanelsMixin,
    AdminTrafficMixin,
    AdminLeaderboardMixin,
):
    """Administrator bot assembled from the admin feature workflows."""

    def __init__(self, sub: Subscription, lang_cfg: ConfigLike, cfg: ConfigLike):
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg = cfg
            self.sub = sub
            self.lang_cfg = lang_cfg
            self.bot = telebot.TeleBot(self.cfg["bot"]["token"])
            self.admin_uids: list[int] = self.cfg["bot"]["whitelist"]
            self._pending_codes: dict[str | int, str] = {}
            self._pending_edits: dict[int, dict[str, str]] = {}
            self._pagination_state: dict[int, dict[str, int]] = {}
            self._pending_leaderboard: dict[int, dict[str, str | int]] = {}
            self.polling_thread: threading.Thread | None = None

            self.bot.message_handler(commands=["start", "menu"])(self.cmd_start)  # pyright: ignore[reportUnknownMemberType]
            self.bot.callback_query_handler(func=lambda call: True)(self.handle_callbacks)  # pyright: ignore[reportUnknownLambdaType, reportUnknownMemberType]


