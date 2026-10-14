from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import MagicMock

import pytest
from bot_helpers import telegram_mock
from telebot.async_telebot import AsyncTeleBot

from bots.telegram.handler_backend import ConversationRouter
from bots.telegram.polling import TelegramPollingMixin, _sanitize_polling_error


class PollingBot(TelegramPollingMixin):
    def __init__(self) -> None:
        self.bot = cast(AsyncTeleBot, telegram_mock())
        self.log = MagicMock()
        self._conversations = ConversationRouter()
        self._initialize_polling()


@pytest.mark.asyncio
async def test_polling_stop_settles_handlers_before_session_owner_closes() -> None:
    bot = PollingBot()
    polling = asyncio.Event()
    release = asyncio.Event()

    async def updates(**kwargs: object) -> list[object]:
        polling.set()
        await asyncio.Event().wait()
        return []

    cast(MagicMock, getattr(bot.bot, "get_updates")).side_effect = updates
    bot._handlers.create(release.wait, name="active handler")
    await bot.start()
    task = bot._polling_task
    await bot.start()
    assert bot._polling_task is task
    await polling.wait()
    close = asyncio.create_task(bot.close())
    await asyncio.sleep(0)
    assert not close.done()
    release.set()
    await close
    await bot.close()
    assert bot._handlers.active_count == 0
    cast(MagicMock, bot.bot.close_session).assert_not_called()


@pytest.mark.asyncio
async def test_polling_errors_are_sanitized_and_retry_is_interruptible() -> None:
    bot = PollingBot()
    token = "123:secret"
    cast(MagicMock, bot.bot).token = token
    called = asyncio.Event()

    async def updates(**kwargs: object) -> list[object]:
        called.set()
        raise OSError(f"https://api.telegram.org/bot{token}/getUpdates")

    cast(MagicMock, getattr(bot.bot, "get_updates")).side_effect = updates
    await bot.start()
    await called.wait()
    await bot.close()
    warning = str(cast(MagicMock, bot.log).warning.call_args)
    assert token not in warning
    assert "secret" not in warning
    assert "{TOKEN}" in warning
    assert "secret" not in _sanitize_polling_error(RuntimeError("secret"), token)
