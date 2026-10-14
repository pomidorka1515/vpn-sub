"""Public bot composition root."""

from __future__ import annotations

from typing import TYPE_CHECKING

from telebot.async_telebot import AsyncTeleBot

from loggers import Logger

from .account import PublicAccountMixin
from .common import PublicCommonMixin
from .login import PublicLoginMixin
from .settings import PublicSettingsMixin
from .subscription import PublicSubscriptionMixin
from .traffic import PublicTrafficMixin

if TYPE_CHECKING:
    from config import AppConfig, LangConfig
    from core import Subscription
    from core.common import Snapshot

__all__ = ["AsyncPublicBot"]


class AsyncPublicBot(
    PublicCommonMixin,
    PublicLoginMixin,
    PublicSettingsMixin,
    PublicSubscriptionMixin,
    PublicAccountMixin,
    PublicTrafficMixin,
):
    """Public bot assembled from the public feature workflows."""

    def __init__(
        self,
        sub: Subscription,
        cfg: Snapshot[AppConfig],
        lang_cfg: Snapshot[LangConfig],
    ) -> None:
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg = cfg
            self.lang_cfg = lang_cfg
            self.sub = sub
            conf = self.cfg.view()
            if "publicbot" not in conf:
                raise KeyError("publicbot")
            token = conf["publicbot"]["token"]
            if not token:
                raise RuntimeError("publicbot.token must not be empty")
            self.bot = AsyncTeleBot(token)
            self._initialize_polling()
            self._initialize_conversations()
            language = lang_cfg.view()
            self.TEXTS: dict[str, dict[str, str]] = language["publicbot"]
            self.bot.message_handler(func=lambda message: True)(self._message_entry)  # type: ignore[no-untyped-call] # pyright: ignore[reportUnknownMemberType]
            self.bot.callback_query_handler(func=lambda call: True)(self._callback_entry)  # type: ignore[no-untyped-call] # pyright: ignore[reportUnknownLambdaType, reportUnknownMemberType]
