from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from bot_helpers import subscription_mock, telegram_mock
from telebot import types

from bots import AsyncAdminBot, AsyncPublicBot
from bots.handler_backend import ConversationRouter

if TYPE_CHECKING:
    from bots.handler_backend import ConversationKey


def message(text: str, *, uid: int = 42) -> types.Message:
    return cast(
        types.Message,
        SimpleNamespace(
            text=text,
            from_user=SimpleNamespace(id=uid),
            chat=SimpleNamespace(id=7),
            message_id=1,
        ),
    )


def public_bot() -> AsyncPublicBot:
    bot = AsyncPublicBot.__new__(AsyncPublicBot)
    bot.bot = telegram_mock()
    bot.sub = subscription_mock()
    bot.log = MagicMock()
    bot.TEXTS = {"en": {"btn_main_back": "Back"}}
    bot._initialize_polling()
    bot._initialize_conversations()
    setattr(bot, "handle_text", AsyncMock())
    setattr(bot, "cmd_start", AsyncMock())
    return bot


@pytest.mark.asyncio
async def test_concurrent_messages_cannot_consume_step_twice() -> None:
    bot = public_bot()
    entered, release = asyncio.Event(), asyncio.Event()
    consumed: list[str | None] = []

    async def step(value: types.Message) -> None:
        consumed.append(value.text)
        entered.set()
        await release.wait()

    bot._conversations.expect((7, 42), step)
    first = asyncio.create_task(bot._message_entry(message("one")))
    await entered.wait()
    second = asyncio.create_task(bot._message_entry(message("two")))
    other = message("other", uid=99)
    await bot._message_entry(other)
    cast(AsyncMock, bot.handle_text).assert_awaited_once_with(other)
    release.set()
    await asyncio.gather(first, second)
    assert consumed == ["one"]
    assert not bot._conversations._locks
    await bot.close()


@pytest.mark.asyncio
async def test_expiry_commands_and_menu_do_not_consume_password_step() -> None:
    bot = public_bot()
    step = AsyncMock()
    bot._conversations = ConversationRouter(ttl=-1)
    bot._conversations.expect((7, 42), step, "email")
    await bot._message_entry(message("ordinary"))
    step.assert_not_awaited()
    bot._conversations = ConversationRouter()
    for text in ("/start", "/menu", "/cancel", "Back"):
        bot._conversations.expect((7, 42), step, "email")
        await bot._message_entry(message(text))
        assert not bot._conversations._states
    step.assert_not_awaited()
    await bot.close()


@pytest.mark.asyncio
async def test_admin_authorization_is_rechecked_at_transition() -> None:
    bot = AsyncAdminBot.__new__(AsyncAdminBot)
    bot.bot = telegram_mock()
    bot.log = MagicMock()
    bot.admin_uids = [42]
    bot._pending_codes = {}
    bot._pending_edits = {}
    bot._pending_leaderboard = {}
    bot._pagination_state = {}
    bot._initialize_polling()
    bot._initialize_conversations()
    step = AsyncMock()
    bot._conversations.expect((7, 42), step)
    bot.admin_uids.clear()
    await bot._message_entry(message("confirm"))
    step.assert_not_awaited()
    assert not bot._conversations._states
    await bot.close()


@pytest.mark.asyncio
async def test_callback_values_are_user_scoped_and_expire() -> None:
    from contextvars import ContextVar

    from bots.pending import PendingValues

    context: ContextVar[ConversationKey] = ContextVar("test conversation")
    values = PendingValues[str](context)
    context.set((7, 42))
    values[7] = "alice"
    context.set((7, 99))
    assert values.get(7) is None
    values[7] = "bob"
    context.set((7, 42))
    assert values[7] == "alice"
    expired = PendingValues[str](context, ttl=-1)
    expired[7] = "secret"
    assert expired.get(7) is None


@pytest.mark.asyncio
async def test_cancelled_polling_waiter_keeps_handler_owned_until_close() -> None:
    bot = public_bot()
    entered, release = asyncio.Event(), asyncio.Event()

    async def step(value: types.Message) -> None:
        entered.set()
        await release.wait()

    bot._conversations.expect((7, 42), step)
    waiter = asyncio.create_task(bot._message_entry(message("confirm")))
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert bot._handlers.active_count == 1
    closing = asyncio.create_task(bot.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await closing
    assert bot._handlers.active_count == 0
    assert not bot._conversations._states
