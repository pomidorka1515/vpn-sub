
from __future__ import annotations

import telebot
import time
import threading
import logging

from loggers import Logger
__all__ = ['TelegramPollingMixin']


_POLLING_CONNECT_TIMEOUT = 5
_POLLING_LONG_TIMEOUT = 5
_POLLING_STOP_TIMEOUT = 6


class TelegramPollingMixin:
    polling_thread: threading.Thread | None = None
    bot: telebot.TeleBot
    log: Logger

    def start_polling(self) -> None:
        if self.polling_thread is not None and self.polling_thread.is_alive():
            return

        self.polling_thread = threading.Thread(
            target=self._poll_forever,
            daemon=True,
            name=f"{type(self).__name__}" # 15-char comm limit
        )
        self.polling_thread.start()

    def _poll_forever(self) -> None:
        self.bot.infinity_polling(  # pyright: ignore[reportUnknownMemberType]
            timeout=_POLLING_CONNECT_TIMEOUT,
            long_polling_timeout=_POLLING_LONG_TIMEOUT,
            logger_level=logging.CRITICAL,
        )

    def stop_polling(self) -> None:
        thread = self.polling_thread
        self.bot.stop_polling()
        if thread is None:
            return

        deadline = time.monotonic() + _POLLING_STOP_TIMEOUT
        while thread.is_alive() and time.monotonic() < deadline:
            thread.join(timeout=0.1)
            self.bot.stop_polling()

        self.polling_thread = None
        if thread.is_alive():
            self.log.error(
                f"polling thread did not stop within {_POLLING_STOP_TIMEOUT} seconds"
            )
