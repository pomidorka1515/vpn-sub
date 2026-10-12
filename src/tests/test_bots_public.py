from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from bot_helpers import subscription_mock, telegram_mock
from helpers import config_mock, language_config, subscription_config
from telebot import types

from bots import AsyncPublicBot


def _public_message(text: str) -> types.Message:
    return cast(
        types.Message,
        SimpleNamespace(text=text, from_user=SimpleNamespace(id=42), chat=SimpleNamespace(id=7)),
    )


@pytest.fixture
def public_bot() -> tuple[AsyncPublicBot, MagicMock, MagicMock]:
    bot = AsyncPublicBot.__new__(AsyncPublicBot)
    telegram = telegram_mock()
    subscription = subscription_mock()
    setattr(bot, "_expect", MagicMock())
    setattr(bot, "_clear_steps", MagicMock())
    bot.bot = telegram
    bot.sub = subscription
    subscription.telegram_svc.get_telegram_language.return_value = "en"
    bot.TEXTS = {
        lang: {button: f"{lang}:{button}" for button, _, _ in bot.ROUTES} for lang in ("ru", "en")
    }
    return bot, telegram, subscription


@pytest.mark.parametrize(
    ("button", "handler_name", "lang"),
    [
        (button, handler_name, lang)
        for button, handler_name, _requires_reg in AsyncPublicBot.ROUTES
        for lang in ("ru", "en")
    ],
)
@pytest.mark.asyncio
async def test_routes_both_languages_to_first_matching_handler(
    public_bot: tuple[AsyncPublicBot, MagicMock, MagicMock],
    button: str,
    handler_name: str,
    lang: str,
) -> None:
    bot, _telegram, _subscription = public_bot
    message = _public_message(bot.TEXTS[lang][button])
    with patch.object(AsyncPublicBot, handler_name) as handler:
        await bot.handle_text(message)
    handler.assert_called_once_with(message, 42, "en")


@pytest.mark.asyncio
async def test_unknown_text_is_ignored(
    public_bot: tuple[AsyncPublicBot, MagicMock, MagicMock],
) -> None:
    bot, telegram, subscription = public_bot
    await bot.handle_text(_public_message("not a button"))
    telegram.send_message.assert_not_called()
    subscription.telegram_svc.is_registered.assert_not_called()


@pytest.mark.asyncio
async def test_chart_buttons_keep_original_order_and_registration_guard(
    public_bot: tuple[AsyncPublicBot, MagicMock, MagicMock],
) -> None:
    bot, telegram, subscription = public_bot
    bot.TEXTS["en"].update(btn_chart_days="Last {days} days", choose_chart_days="Choose a period")
    message = _public_message(bot.TEXTS["ru"]["btn_chart"])
    subscription.telegram_svc.is_registered.return_value = False
    await bot.handle_text(message)
    telegram.send_message.assert_not_called()

    subscription.telegram_svc.is_registered.return_value = True
    await bot.handle_text(message)
    args, kwargs = telegram.send_message.call_args
    assert args == (7, "Choose a period")
    markup = kwargs["reply_markup"]
    assert [button.callback_data for row in markup.keyboard for button in row] == [
        "chart_3",
        "chart_14",
        "chart_30",
        "chart_90",
    ]


@pytest.mark.asyncio
async def test_registered_routes_are_blocked_before_handler_dispatch(
    public_bot: tuple[AsyncPublicBot, MagicMock, MagicMock],
) -> None:
    bot, _telegram, subscription = public_bot
    subscription.telegram_svc.is_registered.return_value = False
    message = _public_message(bot.TEXTS["en"]["btn_info"])
    with patch.object(AsyncPublicBot, "_handle_info") as handler:
        await bot.handle_text(message)
    handler.assert_not_called()
    subscription.telegram_svc.is_registered.assert_called_once_with(42)


@pytest.mark.asyncio
async def test_public_bot_construction_starts_no_work() -> None:
    with patch("bots.public.bot.AsyncTeleBot") as telebot_cls:
        bot = AsyncPublicBot(
            sub=MagicMock(),
            cfg=config_mock(subscription_config(publicbot={"token": "1:token"})),
            lang_cfg=config_mock(language_config()),
        )
    telebot_cls.assert_called_once_with("1:token")
    assert bot._polling_task is None
    assert bot._handlers.active_count == 0
