from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Callable, cast
from unittest.mock import MagicMock, patch

import pytest
from telebot import types

from bots import PublicBot


def _public_message(text: str) -> types.Message:
    return cast(
        types.Message,
        SimpleNamespace(text=text, from_user=SimpleNamespace(id=42), chat=SimpleNamespace(id=7)),
    )


@pytest.fixture
def public_bot() -> tuple[PublicBot, MagicMock, MagicMock]:
    bot = PublicBot.__new__(PublicBot)
    telegram = MagicMock()
    subscription = MagicMock()
    bot.bot = cast(Any, telegram)
    bot.sub = cast(Any, subscription)
    subscription.telegram_svc.get_telegram_language.return_value = "en"
    bot.TEXTS = {
        lang: {button: f"{lang}:{button}" for button, _, _ in bot.ROUTES}
        for lang in ("ru", "en")
    }
    return bot, telegram, subscription


@pytest.mark.parametrize(
    ("button", "handler_name", "lang"),
    [
        (button, handler_name, lang)
        for button, handler_name, _requires_reg in PublicBot.ROUTES
        for lang in ("ru", "en")
    ],
)
def test_routes_both_languages_to_first_matching_handler(
    public_bot: tuple[PublicBot, MagicMock, MagicMock],
    button: str, handler_name: str, lang: str,
) -> None:
    bot, _telegram, _subscription = public_bot
    message = _public_message(bot.TEXTS[lang][button])
    with patch.object(PublicBot, handler_name) as handler:
        bot.handle_text(message)
    handler.assert_called_once_with(message, 42, "en")


def test_unknown_text_is_ignored(
    public_bot: tuple[PublicBot, MagicMock, MagicMock],
) -> None:
    bot, telegram, subscription = public_bot
    bot.handle_text(_public_message("not a button"))
    telegram.send_message.assert_not_called()
    subscription.telegram_svc.is_registered.assert_not_called()


def test_chart_buttons_keep_original_order_and_registration_guard(
    public_bot: tuple[PublicBot, MagicMock, MagicMock],
) -> None:
    bot, telegram, subscription = public_bot
    bot.TEXTS["en"].update(
        btn_chart_days="Last {days} days", choose_chart_days="Choose a period"
    )
    message = _public_message(bot.TEXTS["ru"]["btn_chart"])
    subscription.telegram_svc.is_registered.return_value = False
    bot.handle_text(message)
    telegram.send_message.assert_not_called()

    subscription.telegram_svc.is_registered.return_value = True
    bot.handle_text(message)
    args, kwargs = telegram.send_message.call_args
    assert args == (7, "Choose a period")
    markup = kwargs["reply_markup"]
    assert [button.callback_data for row in markup.keyboard for button in row] == [
        "chart_3", "chart_14", "chart_30", "chart_90",
    ]


def test_registered_routes_are_blocked_before_handler_dispatch(
    public_bot: tuple[PublicBot, MagicMock, MagicMock],
) -> None:
    bot, _telegram, subscription = public_bot
    subscription.telegram_svc.is_registered.return_value = False
    message = _public_message(bot.TEXTS["en"]["btn_info"])
    with patch.object(PublicBot, "_handle_info") as handler:
        bot.handle_text(message)
    handler.assert_not_called()
    subscription.telegram_svc.is_registered.assert_called_once_with(42)


class _ChartOnlyPublicBot(PublicBot):
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1)
        self.stopped_polling = False

    def stop_polling(self) -> None:
        self.stopped_polling = True

    def submit(self, function: Callable[[], bool]) -> Future[bool]:
        return self._executor.submit(function)

    def close(self) -> None:
        self._executor.shutdown(wait=True)


def test_public_bot_stop_does_not_wait_for_running_chart() -> None:
    bot = _ChartOnlyPublicBot()
    release = threading.Event()
    future = bot.submit(release.wait)
    bot.stop()
    assert bot.stopped_polling
    assert not future.done()
    release.set()
    bot.close()
