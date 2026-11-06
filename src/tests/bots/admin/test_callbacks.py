from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telebot import types

from bots.telegram import TelegramAdminBot
from bots.telegram.admin.user_callbacks import user_callback
from errors import AppError
from tests.support.config import config_mock, subscription_config
from tests.support.telegram import subscription_mock, telegram_mock
from util import isusername


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
@pytest.mark.parametrize("length", [54, 55, 58, 59, 60, 64, 200])
async def test_user_callbacks_fit_and_resolve_full_username(
    admin: tuple[TelegramAdminBot, MagicMock, MagicMock],
    length: int,
) -> None:
    bot, telegram, subscription = admin
    username = "a_" + "x" * (length - 2)
    assert isusername(username)
    subscription.user_svc.list_users.return_value = [username]
    for prefix, handler in (("info", "_cb_info_user"), ("dodel", "_cb_del_user")):
        menu = await TelegramAdminBot.get_users_menu(bot, prefix)
        button = menu.inline_keyboard[0][0]
        assert button.text == username
        assert button.callback_data is not None
        assert len(button.callback_data.encode()) <= 64
        # A changed list must not cause an old callback to select another user.
        subscription.user_svc.list_users.return_value = ["bob", username]
        with patch.object(TelegramAdminBot, handler) as action:
            await bot.handle_callbacks(_callback(button.callback_data))
        action.assert_awaited_once_with(7, username)
        subscription.user_svc.list_users.return_value = [username]

    await bot.handle_callbacks(_callback(user_callback("edit_user", username)))
    assert bot._pending_edits[7]["username"] == username
    menu = telegram.send_message.call_args.kwargs["reply_markup"]
    for row in menu.inline_keyboard:
        for edit_button in row:
            assert len(edit_button.callback_data.encode()) <= 64
    generation = bot._pending_edits[7]["generation"]
    with patch.object(TelegramAdminBot, "_cb_edit_limit") as edit:
        await bot.handle_callbacks(_callback(f"edit_limit:{generation}"))
    edit.assert_awaited_once_with(7, username)


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["info", "dodel", "edit_user"])
async def test_removed_user_reference_cannot_retarget_current_workflow(
    admin: tuple[TelegramAdminBot, MagicMock, MagicMock],
    prefix: str,
) -> None:
    bot, telegram, subscription = admin
    await bot._cb_edit_user_options(7, "alice")
    current = bot._pending_edits[7]
    subscription.user_svc.list_users.return_value = ["bob"]
    await bot.handle_callbacks(_callback(user_callback(prefix, "x" * 200)))
    assert "истекла" in telegram.send_message.call_args.args[1]
    assert bot._pending_edits[7] is current
    subscription.business_svc.get_info.assert_not_awaited()
    subscription.business_svc.delete_user.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("next_user", ["bob", "alice"])
async def test_stale_edit_menus_cannot_retarget_or_clear_current_workflow(
    admin: tuple[TelegramAdminBot, MagicMock, MagicMock],
    next_user: str,
) -> None:
    bot, telegram, subscription = admin
    bot.cfg = config_mock(subscription_config(fingerprints=["chrome", "firefox"]))
    subscription.business_svc.get_info.return_value = SimpleNamespace(fingerprint="chrome")
    await bot._cb_edit_user_options(7, "alice")
    generation = bot._pending_edits[7]["generation"]
    options = telegram.send_message.call_args.kwargs["reply_markup"].inline_keyboard
    edit_button = options[0][0].callback_data
    assert edit_button == f"edit_fp:{generation}"
    await bot.handle_callbacks(_callback(edit_button))
    fingerprints = telegram.send_message.call_args.kwargs["reply_markup"].inline_keyboard
    save_button = fingerprints[0][0].callback_data
    assert save_button == f"fp_save_chrome:{generation}"

    await bot._cb_edit_user_options(7, next_user)
    current = bot._pending_edits[7]
    subscription.business_svc.get_info.reset_mock()
    for stale in (edit_button, save_button, "edit_fp", "fp_save_chrome"):
        await bot.handle_callbacks(_callback(stale))
        assert "истекла" in telegram.send_message.call_args.args[1]
        assert bot._pending_edits[7] is current
    subscription.business_svc.get_info.assert_not_awaited()
    subscription.business_svc.update_params.assert_not_awaited()

    await bot.handle_callbacks(_callback(f"fp_save_firefox:{current['generation']}"))
    subscription.business_svc.update_params.assert_awaited_once_with(
        username=next_user,
        fingerprint="firefox",
    )


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
    bot._pending_edits[7] = {"username": "alice", "generation": "test"}
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

    bot._pending_codes[7] = "test:WELCOME"
    await bot.handle_callbacks(_callback("codetype_bonus:test"))
    assert 7 not in bot._pending_codes
    assert bot._step_add_code_days in cast(MagicMock, bot._expect).call_args.args
    assert cast(MagicMock, bot._expect).call_args.args[2:] == ("bonus", "WELCOME")

    await bot.handle_callbacks(_callback("edit_limit"))
    assert "Сессия истекла" in telegram.send_message.call_args.args[1]

    bot._pending_edits[7] = {"username": "alice", "generation": "test"}
    with patch.object(TelegramAdminBot, "_cb_edit_limit") as edit:
        await bot.handle_callbacks(_callback("edit_limit:test"))
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

    bot._pending_edits[7] = {"username": "alice", "generation": "test"}
    subscription.business_svc.update_params.side_effect = AppError("bad fp")
    await bot.handle_callbacks(_callback("fp_save_firefox:test"))
    assert "bad fp" in cast(MagicMock, bot._send_message).call_args.args[1]
    assert 7 not in bot._pending_edits

    bot._pending_edits[7] = {"username": "alice", "generation": "test"}
    subscription.business_svc.update_params.side_effect = None
    await bot.handle_callbacks(_callback("fp_save_firefox:test"))
    subscription.business_svc.update_params.assert_called_with(
        username="alice",
        fingerprint="firefox",
    )
    assert "firefox" in cast(MagicMock, bot._send_message).call_args.args[1]

    await bot.handle_callbacks(_callback("leaderboard"))
    generation = bot._pending_leaderboard[7]["generation"]
    with patch.object(TelegramAdminBot, "_cb_leaderboard_order") as order:
        await bot.handle_callbacks(_callback(f"lbt_monthly:{generation}"))
    assert bot._pending_leaderboard[7]["type"] == "monthly"
    order.assert_called_once_with(7)

    with patch.object(TelegramAdminBot, "_cb_leaderboard_window") as window:
        await bot.handle_callbacks(_callback(f"lbo_asc:{generation}"))
        await bot.handle_callbacks(_callback(f"lbo_desc:{generation}"))
    assert bot._pending_leaderboard[7]["order"] == "asc"
    assert window.call_count == 1


@pytest.mark.asyncio
async def test_stale_code_type_and_cancel_cannot_consume_new_wizard(
    admin: tuple[TelegramAdminBot, MagicMock, MagicMock],
) -> None:
    bot, telegram, _subscription = admin
    message = cast(types.Message, SimpleNamespace(text="FIRST", chat=SimpleNamespace(id=7)))
    await bot._step_add_code_name(message)
    old_buttons = [
        button.callback_data
        for row in telegram.send_message.call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    await bot.handle_callbacks(_callback("add_code"))
    assert 7 not in bot._pending_codes
    message.text = "NEW:CODE"
    await bot._step_add_code_name(message)
    current = bot._pending_codes[7]
    new_buttons = [
        button.callback_data
        for row in telegram.send_message.call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert old_buttons != new_buttons
    for stale in [*old_buttons, "codetype_bonus", "codetype_bad:" + current.split(":", 1)[0]]:
        await bot.handle_callbacks(_callback(stale))
        assert "истекла" in telegram.send_message.call_args.args[1]
        assert bot._pending_codes[7] == current
    await bot.handle_callbacks(_callback(new_buttons[1]))
    assert 7 not in bot._pending_codes
    assert cast(MagicMock, bot._expect).call_args.args[2:] == ("bonus", "NEW:CODE")
    await bot.handle_callbacks(_callback(new_buttons[0]))
    assert "истекла" in telegram.send_message.call_args.args[1]
    await bot._step_add_code_name(message)
    cancel_button = telegram.send_message.call_args.kwargs["reply_markup"].inline_keyboard[1][0]
    await bot.handle_callbacks(_callback(cancel_button.callback_data))
    assert 7 not in bot._pending_codes
    telegram.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_leaderboard_selections_are_bound_to_generation_and_stage(
    admin: tuple[TelegramAdminBot, MagicMock, MagicMock],
) -> None:
    bot, telegram, _subscription = admin
    await bot.handle_callbacks(_callback("leaderboard"))
    old_type = (
        telegram.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    )
    await bot.handle_callbacks(_callback(old_type))
    old_order = (
        telegram.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    )
    await bot.handle_callbacks(_callback("leaderboard"))
    new_type = (
        telegram.send_message.call_args.kwargs["reply_markup"].inline_keyboard[1][0].callback_data
    )
    current = bot._pending_leaderboard[7]
    for stale in (old_type, old_order, "lbt_total", "lbo_desc"):
        await bot.handle_callbacks(_callback(stale))
        assert "истекла" in telegram.send_message.call_args.args[1]
        assert current == {"generation": new_type.split(":")[1], "stage": "type"}
    await bot.handle_callbacks(_callback(new_type))
    new_order = (
        telegram.send_message.call_args.kwargs["reply_markup"].inline_keyboard[1][0].callback_data
    )
    assert len(new_order.encode()) <= 64
    await bot.handle_callbacks(_callback(old_order))
    assert current["stage"] == "order"
    await bot.handle_callbacks(_callback(new_type))
    assert current["stage"] == "order"
    await bot.handle_callbacks(_callback(new_order))
    assert current["stage"] == "window"
    assert current["type"] == "monthly"
    assert current["order"] == "asc"
    await bot.handle_callbacks(_callback(new_order))
    assert current["stage"] == "window"

    old_generation = cast(str, current["generation"])
    await bot.handle_callbacks(_callback("leaderboard"))
    latest = bot._pending_leaderboard[7]
    message = cast(types.Message, SimpleNamespace(text="3", chat=SimpleNamespace(id=7)))
    await bot._step_leaderboard_window(message, old_generation)
    assert "истекла" in telegram.send_message.call_args.args[1]
    assert bot._pending_leaderboard[7] is latest
