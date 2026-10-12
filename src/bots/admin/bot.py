"""Administrator bot composition root."""

from __future__ import annotations

from typing import TYPE_CHECKING

from telebot.async_telebot import AsyncTeleBot

from bots.pending import PendingValues
from loggers import Logger

from .codes import AdminCodesMixin
from .common import AdminCommonMixin
from .leaderboard import AdminLeaderboardMixin
from .panels import AdminPanelsMixin
from .traffic import AdminTrafficMixin
from .users import AdminUsersMixin

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from config import AppConfig, LangConfig
    from core import AsyncSubscription
    from core.async_common import Snapshot

__all__ = ["AsyncAdminBot"]


class AsyncAdminBot(
    AdminCommonMixin,
    AdminUsersMixin,
    AdminCodesMixin,
    AdminPanelsMixin,
    AdminTrafficMixin,
    AdminLeaderboardMixin,
):
    """Administrator bot assembled from the admin feature workflows."""

    def __init__(
        self, sub: AsyncSubscription, lang_cfg: Snapshot[LangConfig], cfg: Snapshot[AppConfig],
    ) -> None:
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg = cfg
            self.sub = sub
            self.lang_cfg = lang_cfg
            conf = self.cfg.view()
            if "bot" not in conf:
                raise KeyError("bot")
            self.bot = AsyncTeleBot(conf["bot"]["token"])
            self.admin_uids: list[int] = conf["bot"]["whitelist"]
            self._initialize_polling()
            self._initialize_conversations()
            self._pending_codes: MutableMapping[str | int, str] = PendingValues[str](self._conversation)
            self._pending_edits: MutableMapping[str | int, dict[str, str]] = PendingValues[dict[str, str]](self._conversation)
            self._pagination_state: MutableMapping[str | int, dict[str, int]] = PendingValues[dict[str, int]](self._conversation)
            self._pending_leaderboard: MutableMapping[str | int, dict[str, str | int]] = PendingValues[dict[str, str | int]](self._conversation)

            self.bot.message_handler(func=lambda message: True)(self._message_entry)  # type: ignore[no-untyped-call] # pyright: ignore[reportUnknownMemberType]
            self.bot.callback_query_handler(func=lambda call: True)(self._callback_entry)  # type: ignore[no-untyped-call] # pyright: ignore[reportUnknownLambdaType, reportUnknownMemberType]
