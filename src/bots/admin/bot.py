"""Administrator bot composition root."""

from __future__ import annotations

import threading

import telebot

from bots.polling import configure_telegram_api
from config import AppConfig, Config, LangConfig
from core import Subscription
from loggers import Logger

from .codes import AdminCodesMixin
from .common import AdminCommonMixin
from .leaderboard import AdminLeaderboardMixin
from .panels import AdminPanelsMixin
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
):
    """Administrator bot assembled from the admin feature workflows."""

    def __init__(self, sub: Subscription, lang_cfg: Config[LangConfig], cfg: Config[AppConfig]):
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            configure_telegram_api()
            self.cfg = cfg
            self.sub = sub
            self.lang_cfg = lang_cfg
            conf = self.cfg.view()
            if "bot" not in conf:
                raise KeyError("bot")
            self.bot = telebot.TeleBot(conf["bot"]["token"])
            self.admin_uids: list[int] = conf["bot"]["whitelist"]
            self._pending_codes: dict[str | int, str] = {}
            self._pending_edits: dict[int, dict[str, str]] = {}
            self._pagination_state: dict[int, dict[str, int]] = {}
            self._pending_leaderboard: dict[int, dict[str, str | int]] = {}
            self.polling_thread: threading.Thread | None = None

            self.bot.message_handler(commands=["start", "menu"])(self.cmd_start)  # pyright: ignore[reportUnknownMemberType]
            self.bot.callback_query_handler(func=lambda call: True)(self.handle_callbacks)  # type: ignore[no-untyped-call] # pyright: ignore[reportUnknownLambdaType, reportUnknownMemberType]
