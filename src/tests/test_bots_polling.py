from __future__ import annotations

import threading
import time
from typing import Any, cast

import pytest

from bots.polling import TelegramPollingMixin


_STOP_WAIT_SECONDS = 30


class _NullLogger:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warning(self, message: str, *args: object) -> None:
        if args:
            message = message % args
        self.warnings.append(message)


class _Poller:
    def __init__(self, stop_event: threading.Event) -> None:
        self.stop_polling_calls = 0
        self.polling_started = threading.Event()
        self.token: str | None = None
        self._stop_event = stop_event

    def infinity_polling(self, **kwargs: Any) -> None:
        self.polling_started.set()
        self._stop_event.wait(_STOP_WAIT_SECONDS)

    def stop_polling(self) -> None:
        self.stop_polling_calls += 1


class _TestBot(TelegramPollingMixin):
    def __init__(self, stop_event: threading.Event) -> None:
        self.poller = _Poller(stop_event)
        self.logger = _NullLogger()
        self.bot = cast(Any, self.poller)
        self.log = cast(Any, self.logger)


@pytest.fixture
def stop_event() -> threading.Event:
    return threading.Event()


@pytest.fixture
def polling_bot(stop_event: threading.Event) -> _TestBot:
    return _TestBot(stop_event)


def test_start_polling_reuses_running_thread(
    polling_bot: _TestBot, stop_event: threading.Event,
) -> None:
    polling_bot.start_polling()
    assert polling_bot.polling_thread is not None
    thread = polling_bot.polling_thread
    assert polling_bot.poller.polling_started.wait(1)

    polling_bot.start_polling()
    assert polling_bot.polling_thread is thread

    stop_event.set()
    polling_bot.stop_polling()
    assert not thread.is_alive()
    assert polling_bot.logger.errors == []


def test_stop_polling_is_idempotent_and_does_not_join_without_thread(
    polling_bot: _TestBot, stop_event: threading.Event,
) -> None:
    started = time.monotonic()
    polling_bot.stop_polling()
    assert time.monotonic() - started < 0.1
    assert polling_bot.poller.stop_polling_calls == 1

    polling_bot.start_polling()
    assert polling_bot.poller.polling_started.wait(1)
    stop_event.set()
    polling_bot.stop_polling()
    polling_bot.stop_polling()
    assert polling_bot.poller.stop_polling_calls >= 3


def test_stop_polling_logs_timeout(
    polling_bot: _TestBot, stop_event: threading.Event,
) -> None:
    polling_bot.start_polling()
    assert polling_bot.polling_thread is not None
    thread = polling_bot.polling_thread
    assert polling_bot.poller.polling_started.wait(1)

    polling_bot.stop_polling()
    assert thread.is_alive()
    assert len(polling_bot.logger.errors) == 1

    stop_event.set()
    thread.join(_STOP_WAIT_SECONDS)
    assert not thread.is_alive()


def test_polling_errors_are_logged_without_token_or_traceback(
    polling_bot: _TestBot, stop_event: threading.Event,
) -> None:
    token = "123456:secret-token-value"
    polling_bot.poller.token = token
    raised = threading.Event()

    def infinity_polling(**kwargs: Any) -> None:
        if not raised.is_set():
            raised.set()
            raise ConnectionError(
                f"HTTPSConnectionPool(host='api.telegram.org', port=443): "
                f"Max retries exceeded with url: /bot{token}/getUpdates "
                f"(Caused by ProtocolError('Connection aborted.', "
                f"ConnectionResetError(104, 'Connection reset by peer')))"
            )
        stop_event.wait(_STOP_WAIT_SECONDS)

    polling_bot.poller.infinity_polling = infinity_polling  # type: ignore[method-assign]
    polling_bot.start_polling()
    assert raised.wait(1)
    deadline = time.monotonic() + 1
    while not polling_bot.logger.warnings and time.monotonic() < deadline:
        time.sleep(0.01)

    assert len(polling_bot.logger.warnings) == 1
    warning = polling_bot.logger.warnings[0]
    assert "telegram polling failed:" in warning
    assert "ConnectionError" in warning
    assert token not in warning
    assert "secret-token-value" not in warning
    assert "Traceback" not in warning
    assert polling_bot.logger.errors == []

    stop_event.set()
    polling_bot.stop_polling()
