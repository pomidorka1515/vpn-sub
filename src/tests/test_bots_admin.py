from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from telebot import types

from bots import AdminBot


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
def admin_bot() -> tuple[AdminBot, MagicMock]:
    bot = AdminBot.__new__(AdminBot)
    telegram = MagicMock()
    bot.bot = cast(Any, telegram)
    bot.admin_uids = [42]
    bot.log = MagicMock()
    return bot, telegram


@pytest.mark.parametrize(("route", "handler_name", "is_prefix"), AdminBot.ROUTES)
def test_routes_exact_and_prefix_callbacks_to_first_matching_handler(
    admin_bot: tuple[AdminBot, MagicMock], route: str, handler_name: str, is_prefix: bool,
) -> None:
    bot, _telegram = admin_bot
    data = f"{route}value" if is_prefix else route
    call = _callback(data)
    with patch.object(AdminBot, handler_name) as handler:
        bot.handle_callbacks(call)
    handler.assert_called_once_with(data, 7, call.message)


def test_overlapping_routes_keep_specific_callbacks_first(
    admin_bot: tuple[AdminBot, MagicMock],
) -> None:
    bot, _telegram = admin_bot
    call = _callback("info_code")
    with (
        patch.object(AdminBot, "_handle_info_code") as info_code,
        patch.object(AdminBot, "_handle_info_user") as info_user,
    ):
        bot.handle_callbacks(call)
    info_code.assert_called_once_with("info_code", 7, call.message)
    info_user.assert_not_called()

    call = _callback("edit_user_alice")
    with (
        patch.object(AdminBot, "_handle_edit_user") as edit_user,
        patch.object(AdminBot, "_handle_edit") as edit_action,
    ):
        bot.handle_callbacks(call)
    edit_user.assert_called_once_with("edit_user_alice", 7, call.message)
    edit_action.assert_not_called()


def test_unknown_callback_is_acknowledged_and_ignored(
    admin_bot: tuple[AdminBot, MagicMock],
) -> None:
    bot, telegram = admin_bot
    bot.handle_callbacks(_callback("unknown"))
    telegram.answer_callback_query.assert_called_once_with("callback-id")
    telegram.send_message.assert_not_called()
    cast(MagicMock, bot.log).error.assert_not_called()


def test_non_admin_callback_is_ignored_without_acknowledgement(
    admin_bot: tuple[AdminBot, MagicMock],
) -> None:
    bot, telegram = admin_bot
    bot.handle_callbacks(_callback("list_users", user_id=99))
    telegram.answer_callback_query.assert_not_called()
