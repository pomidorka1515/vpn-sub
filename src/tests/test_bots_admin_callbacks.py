from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bot_helpers import subscription_mock, telegram_mock
from telebot import types

from bots.telegram import TelegramAdminBot
from errors import AppError


def _callback(data: str, user_id: int = 42) -> types.CallbackQuery:
    return cast(
        types.CallbackQuery,
        SimpleNamespace(
            id="callback-id",
            data=data,
            from_user=SimpleNamespace(id=user_id),
            message=SimpleNamespace(message_id=11, chat=SimpleNamespace(id=7)),
        ),
    )


@pytest.fixture
def admin() -> tuple[TelegramAdminBot, MagicMock, MagicMock]:
    bot = TelegramAdminBot.__new__(TelegramAdminBot)
    telegram = telegram_mock()
    subscription = subscription_mock()
    setattr(bot, "_expect", MagicMock())
    setattr(bot, "_clear_steps", MagicMock())
    bot.bot = telegram
    bot.sub = subscription
    bot.log = MagicMock()
    bot.admin_uids = [42]
    bot._pending_edits = {}
    bot._pending_codes = {}
    bot._pagination_state = {}
    bot._pending_leaderboard = {}
    bot.get_main_menu = MagicMock(return_value="main")  # type: ignore[method-assign]
    bot.get_codes_menu = MagicMock(return_value="codes")  # type: ignore[method-assign]
    bot.get_users_menu = AsyncMock(return_value="users")  # type: ignore[method-assign]
    bot._send_message = AsyncMock()  # type: ignore[method-assign]
    bot._answer_callback = AsyncMock()  # type: ignore[method-assign]
    telegram.send_message.return_value = SimpleNamespace(message_id=3)
    return bot, telegram, subscription


@pytest.mark.asyncio
async def test_callback_exception_is_reported(
    admin: tuple[TelegramAdminBot, MagicMock, MagicMock],
) -> None:
    bot, _telegram, _subscription = admin
    with patch.object(TelegramAdminBot, "_handle_cancel", side_effect=RuntimeError("boom")):
        await bot.handle_callbacks(_callback("cancel"))
    cast(MagicMock, bot.log).exception.assert_called()
    cast(MagicMock, bot._send_message).assert_called_once_with(7, "⚠️ Внутренняя ошибка")


@pytest.mark.asyncio
async def test_cancel_clears_pending_edit_and_step(
    admin: tuple[TelegramAdminBot, MagicMock, MagicMock],
) -> None:
    bot, telegram, _subscription = admin
    bot._pending_edits[7] = {"username": "alice"}
    await bot.handle_callbacks(_callback("cancel"))
    assert 7 not in bot._pending_edits
    telegram.edit_message_text.assert_called_once()
    cast(MagicMock, bot._clear_steps).assert_called_once_with(7)
    await bot.handle_callbacks(_callback("noop"))
    telegram.edit_message_text.assert_called_once()


@pytest.mark.asyncio
async def test_menus_prompt_next_step_or_report_empty(
    admin: tuple[TelegramAdminBot, MagicMock, MagicMock],
) -> None:
    bot, telegram, subscription = admin
    subscription.user_svc.list_users.return_value = []
    await bot.handle_callbacks(_callback("info_user"))
    await bot.handle_callbacks(_callback("action_del"))
    assert "пуст" in telegram.send_message.call_args.args[1]

    subscription.user_svc.list_users.return_value = ["alice"]
    await bot.handle_callbacks(_callback("info_user"))
    await bot.handle_callbacks(_callback("page_info_2"))
    assert bot._pagination_state[7]["info_page"] == 2
    await bot.handle_callbacks(_callback("action_del"))
    await bot.handle_callbacks(_callback("page_dodel_1"))
    assert bot._pagination_state[7]["del_page"] == 1

    for data, handler in (
        ("reset_user", bot._step_reset_user),
        ("add_user", bot._step_add_user_name),
        ("info_code", bot._step_info_code),
        ("del_code", bot._step_del_code),
        ("add_code", bot._step_add_code_name),
        ("chart", bot._step_chart_username),
    ):
        await bot.handle_callbacks(_callback(data))
        registered = cast(
            Callable[[types.Message], None],
            cast(MagicMock, bot._expect).call_args.args[1],
        )
        assert getattr(registered, "__func__", registered) is getattr(handler, "__func__", handler)


@pytest.mark.asyncio
async def test_code_type_and_edit_sessions_expire(
    admin: tuple[TelegramAdminBot, MagicMock, MagicMock],
) -> None:
    bot, telegram, _subscription = admin
    await bot.handle_callbacks(_callback("codetype_bonus"))
    assert "истекла" in telegram.send_message.call_args.args[1]

    bot._pending_codes[7] = "WELCOME"
    await bot.handle_callbacks(_callback("codetype_bonus"))
    assert 7 not in bot._pending_codes
    assert bot._step_add_code_days in cast(MagicMock, bot._expect).call_args.args
    assert cast(MagicMock, bot._expect).call_args.args[2:] == ("bonus", "WELCOME")

    await bot.handle_callbacks(_callback("edit_limit"))
    assert "Сессия истекла" in telegram.send_message.call_args.args[1]

    bot._pending_edits[7] = {"username": "alice"}
    with patch.object(TelegramAdminBot, "_cb_edit_limit") as edit:
        await bot.handle_callbacks(_callback("edit_limit"))
    edit.assert_called_once_with(7, "alice")

    await bot.handle_callbacks(_callback("edit_unknown"))
    telegram.send_message.assert_called()


@pytest.mark.asyncio
async def test_fingerprint_save_and_leaderboard_order(
    admin: tuple[TelegramAdminBot, MagicMock, MagicMock],
) -> None:
    bot, telegram, subscription = admin
    await bot.handle_callbacks(_callback("fp_save_firefox"))
    assert "Сессия истекла" in telegram.send_message.call_args.args[1]

    bot._pending_edits[7] = {"username": "alice"}
    subscription.business_svc.update_params.side_effect = AppError("bad fp")
    await bot.handle_callbacks(_callback("fp_save_firefox"))
    assert "bad fp" in cast(MagicMock, bot._send_message).call_args.args[1]
    assert 7 not in bot._pending_edits

    bot._pending_edits[7] = {"username": "alice"}
    subscription.business_svc.update_params.side_effect = None
    await bot.handle_callbacks(_callback("fp_save_firefox"))
    subscription.business_svc.update_params.assert_called_with(
        username="alice",
        fingerprint="firefox",
    )
    assert "firefox" in cast(MagicMock, bot._send_message).call_args.args[1]

    with patch.object(TelegramAdminBot, "_cb_leaderboard_order") as order:
        await bot.handle_callbacks(_callback("lbt_monthly"))
    assert bot._pending_leaderboard[7] == {"type": "monthly"}
    order.assert_called_once_with(7)

    with patch.object(TelegramAdminBot, "_cb_leaderboard_window") as window:
        await bot.handle_callbacks(_callback("lbo_asc"))
        await bot.handle_callbacks(_callback("lbo_desc"))
    assert bot._pending_leaderboard[7]["order"] == "desc"
    assert window.call_count == 2
