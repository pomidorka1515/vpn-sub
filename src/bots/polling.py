from __future__ import annotations

import re
import telebot
import telebot.apihelper as apihelper
import time
import threading
from typing import cast

from requests.exceptions import RequestException

from loggers import Logger
__all__ = ['TelegramPollingMixin', 'configure_telegram_api']


_POLLING_CONNECT_TIMEOUT = 20
_POLLING_LONG_TIMEOUT = 20
_POLLING_STOP_TIMEOUT = 6
_POLLING_RETRY_DELAY = 3
_TRANSIENT_API_CODES = frozenset({429, 500, 502, 503, 504})
_BOT_TOKEN_IN_URL = re.compile(r"/bot\d+:[A-Za-z0-9_-]+", re.IGNORECASE)


def configure_telegram_api() -> None:
    """Tune the process-wide pyTelegramBotAPI client before either bot starts.

    Both bots share one session. Recycling it every 10 minutes, and failing
    the first dropped long-poll with no retry, is what surfaces as errno 104.
    """
    apihelper.RETRY_ON_ERROR = True
    apihelper.RETRY_TIMEOUT = 1
    apihelper.MAX_RETRIES = 3
    apihelper.CONNECT_TIMEOUT = _POLLING_CONNECT_TIMEOUT
    # pyTelegramBotAPI types this as int; None disables the 10-minute recycle.
    apihelper.SESSION_TIME_TO_LIVE = cast(int, None)


def _sanitize_polling_error(error: BaseException, token: object) -> str:
    text = f"{type(error).__name__}: {error}"
    if isinstance(token, str) and token:
        text = text.replace(token, "{TOKEN}")
        _id, separator, secret = token.partition(":")
        if separator and secret:
            text = text.replace(secret, "{TOKEN}")
    return _BOT_TOKEN_IN_URL.sub("/bot{TOKEN}", text)


def _log_polling_error(log: Logger, error: BaseException, token: object) -> None:
    log.warning("telegram polling failed: %s", _sanitize_polling_error(error, token))


class TelegramPollingMixin:
    polling_thread: threading.Thread | None = None
    bot: telebot.TeleBot
    log: Logger
    _polling_stop: threading.Event | None = None

    def start_polling(self) -> None:
        if self.polling_thread is not None and self.polling_thread.is_alive():
            return

        stop = self._ensure_polling_stop()
        stop.clear()
        self.polling_thread = threading.Thread(
            target=self._poll_forever,
            daemon=True,
            name=f"{type(self).__name__}" # 15-char comm limit
        )
        self.polling_thread.start()

    def _ensure_polling_stop(self) -> threading.Event:
        stop = self._polling_stop
        if stop is None:
            stop = threading.Event()
            self._polling_stop = stop
        return stop

    def _poll_forever(self) -> None:
        handler = _PollingExceptionHandler(self.log, getattr(self.bot, "token", None))
        self.bot.exception_handler = cast(telebot.ExceptionHandler, handler)
        stop = self._ensure_polling_stop()
        while not stop.is_set():
            try:
                self.bot.infinity_polling(  # pyright: ignore[reportUnknownMemberType]
                    timeout=_POLLING_CONNECT_TIMEOUT,
                    long_polling_timeout=_POLLING_LONG_TIMEOUT,
                    logger_level=None,
                )
            except Exception as error:
                if stop.is_set():
                    return
                _log_polling_error(self.log, error, getattr(self.bot, "token", None))
                stop.wait(_POLLING_RETRY_DELAY)
            else:
                return

    def stop_polling(self) -> None:
        thread = self.polling_thread
        self._ensure_polling_stop().set()
        self.bot.stop_polling()  # type: ignore[no-untyped-call]  # Upstream method has no annotations.
        if thread is None:
            return

        deadline = time.monotonic() + _POLLING_STOP_TIMEOUT
        while thread.is_alive() and time.monotonic() < deadline:
            thread.join(timeout=0.1)
            self.bot.stop_polling()  # type: ignore[no-untyped-call]

        self.polling_thread = None
        if thread.is_alive():
            self.log.error(
                f"polling thread did not stop within {_POLLING_STOP_TIMEOUT} seconds"
            )


def _is_transient_polling_error(error: BaseException) -> bool:
    """Errors the library must back off itself. Handling them skips that backoff."""
    if isinstance(error, (RequestException, TimeoutError, ConnectionError, OSError)):
        return True
    error_code = getattr(error, "error_code", None)
    if isinstance(error_code, int) and error_code in _TRANSIENT_API_CODES:
        return True
    result = getattr(error, "result", None)
    status = getattr(result, "status_code", None)
    return isinstance(status, int) and status in _TRANSIENT_API_CODES


class _PollingExceptionHandler:
    def __init__(self, log: Logger, token: object) -> None:
        self._log = log
        self._token = token

    def handle(self, exception: Exception) -> bool:
        if _is_transient_polling_error(exception):
            return False
        _log_polling_error(self._log, exception, self._token)
        return True
