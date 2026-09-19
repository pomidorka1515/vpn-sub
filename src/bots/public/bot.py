"""Public bot composition root."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import telebot

from config import ConfigLike
from core import Subscription
from loggers import Logger

from .common import PublicCommonMixin
from .login import PublicLoginMixin
from .settings import PublicSettingsMixin
from .subscription import PublicSubscriptionMixin
from .account import PublicAccountMixin
from .traffic import PublicTrafficMixin
__all__ = ["PublicBot"]



class PublicBot(
    PublicCommonMixin,
    PublicLoginMixin,
    PublicSettingsMixin,
    PublicSubscriptionMixin,
    PublicAccountMixin,
    PublicTrafficMixin,
):
    """Public bot assembled from the public feature workflows."""

    def __init__(self, sub: Subscription, cfg: ConfigLike, lang_cfg: ConfigLike):
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg = cfg
            self.lang_cfg = lang_cfg
            self.sub = sub
            token: str = self.cfg["publicbot"].get("token")
            if not token:
                raise RuntimeError("public bot token not found in config.json")
            self.bot = telebot.TeleBot(token)
            self.TEXTS: dict[str, dict[str, str]] = lang_cfg["publicbot"]
            self.bot.message_handler(commands=["start", "menu"])(self.cmd_start)  # pyright: ignore[reportUnknownMemberType]
            callbacks: tuple[tuple[str, Callable[..., Any]], ...] = (
                ("lang_", self.set_lang_callback),
                ("set_", self.settings_callback),
                ("fp_", self.fp_callback),
                ("login_", self.login_callback),
                ("chart_", self.chart_callback),
            )
            for prefix, handler in callbacks:
                self.bot.callback_query_handler(  # pyright: ignore[reportUnknownMemberType]
                    func=lambda call, p=prefix: call.data.startswith(p)  # pyright: ignore[reportUnknownLambdaType, reportUnknownMemberType]
                )(handler)  # pyright: ignore[reportUnknownLambdaType]
            self.bot.message_handler(func=lambda g: True)(self.handle_text)  # pyright: ignore[reportUnknownLambdaType, reportUnknownMemberType]
            self._executor = ThreadPoolExecutor(
                max_workers=15, thread_name_prefix=f"{type(self).__name__}-chart"
            )
            self.polling_thread: threading.Thread | None = None


