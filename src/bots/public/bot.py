"""Public bot composition root."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import telebot
from telebot import types

from bots.handler_backend import LockedHandlerBackend
from bots.polling import configure_telegram_api
from loggers import Logger

from .account import PublicAccountMixin
from .common import PublicCommonMixin
from .login import PublicLoginMixin
from .settings import PublicSettingsMixin
from .subscription import PublicSubscriptionMixin
from .traffic import PublicTrafficMixin

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable

    from config import AppConfig, Config, LangConfig
    from core import Subscription

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

    def __init__(self, sub: Subscription, cfg: Config[AppConfig], lang_cfg: Config[LangConfig]) -> None:
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            configure_telegram_api()
            self.cfg = cfg
            self.lang_cfg = lang_cfg
            self.sub = sub
            conf = self.cfg.view()
            if "publicbot" not in conf:
                raise KeyError("publicbot")
            token = conf["publicbot"]["token"]
            if not token:
                raise RuntimeError("publicbot.token must not be empty")
            step_backend = LockedHandlerBackend()
            self.bot = telebot.TeleBot(
                token,
                threaded=True,
                num_threads=3,
                next_step_backend=step_backend,
                reply_backend=step_backend,
            )
            language = lang_cfg.view()
            self.TEXTS: dict[str, dict[str, str]] = language["publicbot"]
            self.bot.message_handler(commands=["start", "menu"])(self.cmd_start)  # pyright: ignore[reportUnknownMemberType]
            callbacks: tuple[tuple[str, Callable[[types.CallbackQuery], None]], ...] = (
                ("lang_", self.set_lang_callback),
                ("set_", self.settings_callback),
                ("fp_", self.fp_callback),
                ("login_", self.login_callback),
                ("chart_", self.chart_callback),
            )
            for prefix, handler in callbacks:
                self.bot.callback_query_handler(  # type: ignore[no-untyped-call] # pyright: ignore[reportUnknownMemberType]
                    func=lambda call, p=prefix: call.data.startswith(p)  # pyright: ignore[reportUnknownLambdaType, reportUnknownMemberType]
                )(handler)  # pyright: ignore[reportUnknownLambdaType]
            self.bot.message_handler(func=lambda g: True)(self.handle_text)  # pyright: ignore[reportUnknownLambdaType, reportUnknownMemberType]
            self._executor = ThreadPoolExecutor(
                max_workers=15, thread_name_prefix=f"{type(self).__name__}-chart"
            )
            self.polling_thread: threading.Thread | None = None
