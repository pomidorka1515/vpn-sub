from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from telebot import types

from bots.admin.users import AdminUsersMixin
from custom_types import UserInfo, UserInfoBandwidth, UserInfoBandwidthTotal
from errors import AppError, NotFoundError, PanelUnavailableError
from helpers import config_mock, subscription_config


def _message(text: str, chat_id: int = 7) -> types.Message:
    return cast(
        types.Message,
        SimpleNamespace(text=text, chat=SimpleNamespace(id=chat_id), message_id=11),
    )


def _info(**overrides: Any) -> UserInfo:
    bandwidth = UserInfoBandwidth(
        total=UserInfoBandwidthTotal(upload=12, download=34, total=46),
        wl_total=UserInfoBandwidthTotal(upload=1, download=2, total=3),
        monthly=100,
        wl_monthly=0,
        limit=5,
        wl_limit=0,
    )
    payload: dict[str, Any] = {
        "_": "test",
        "token": "t" * 40,
        "link": "https://example.test/sub?token=t",
        "displayname": "Alice",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "fingerprint": "chrome",
        "enabled": True,
        "wl_enabled": False,
        "time": 1_700_000_000,
        "online": True,
        "bandwidth": bandwidth,
    }
    payload.update(overrides)
    return UserInfo(**payload)


@pytest.fixture
def users() -> tuple[AdminUsersMixin, MagicMock, MagicMock]:
    mixin = AdminUsersMixin.__new__(AdminUsersMixin)
    telegram = MagicMock()
    subscription = MagicMock()
    mixin.bot = cast(Any, telegram)
    mixin.sub = cast(Any, subscription)
    mixin.log = MagicMock()
    mixin.cfg = config_mock(subscription_config(fingerprints=["chrome", "firefox"]))
    mixin.USERS_PER_PAGE = 2
    mixin._pending_edits = {}
    mixin.get_main_menu = MagicMock(return_value="menu")  # type: ignore[method-assign]
    mixin._send_message = MagicMock()  # type: ignore[method-assign]
    telegram.send_message.return_value = _message("prompt")
    return mixin, telegram, subscription


def _buttons(markup: object) -> list[str]:
    keyboard = cast(Any, markup).inline_keyboard
    return [button.callback_data for row in keyboard for button in row]


def test_list_users_empty_and_paginated(
    users: tuple[AdminUsersMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = users
    subscription.user_svc.list_users.return_value = []
    mixin._cb_list_users(7)
    telegram.send_message.assert_called_once()
    assert "пуст" in telegram.send_message.call_args.args[1]

    subscription.user_svc.list_users.return_value = ["a", "b", "c"]
    mixin._cb_list_users(7, page=99)
    text = cast(MagicMock, mixin._send_message).call_args.args[1]
    assert "<code>c</code>" in text
    assert "<code>a</code>" not in text
    assert "page_list_users_0" in _buttons(cast(MagicMock, mixin._send_message).call_args.kwargs["reply_markup"])


def test_online_users_and_refresh_failures(
    users: tuple[AdminUsersMixin, MagicMock, MagicMock],
) -> None:
    mixin, _telegram, subscription = users
    subscription.panel_svc.get_online_users.return_value = {}
    mixin._cb_online_users(7)
    assert "Нет пользователей" in cast(MagicMock, mixin._send_message).call_args.args[1]

    subscription.panel_svc.get_online_users.return_value = {"alice": "tg", "bob": ""}
    mixin._cb_online_users(7)
    text = cast(MagicMock, mixin._send_message).call_args.args[1]
    assert "логин: tg" in text
    assert "bob" in text

    subscription.user_svc.list_users.return_value = ["alice", "bob", "carol"]
    subscription.panel_svc.client_maps.return_value = {}

    def refresh(username: str, *args: object, **kwargs: object) -> None:
        if username == "bob":
            raise PanelUnavailableError("down")
        if username == "carol":
            raise RuntimeError("boom")

    subscription.business_svc.add_users.side_effect = refresh
    mixin._cb_refresh(7)
    aborted = cast(MagicMock, mixin._send_message).call_args.args[1]
    assert "прервано" in aborted
    assert "панель недоступна: 1" in aborted

    subscription.business_svc.add_users.side_effect = PanelUnavailableError("down")
    mixin._cb_refresh(7)
    assert "bob" in cast(MagicMock, mixin._send_message).call_args.args[1]

    subscription.business_svc.add_users.side_effect = None
    mixin._cb_refresh(7)
    assert "успешно" in cast(MagicMock, mixin._send_message).call_args.args[1]


def test_user_info_renders_expiry_and_errors(
    users: tuple[AdminUsersMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = users
    subscription.business_svc.get_info.return_value = _info()
    mixin._cb_info_user(7, "alice")
    text = telegram.send_message.call_args.args[1]
    assert "14.11.23 22:13 (UTC)" in text
    assert "0 MB" in text
    assert "Статус WL: 🔴 Отключен" in text
    assert "edit_user_alice" in _buttons(telegram.send_message.call_args.kwargs["reply_markup"])

    subscription.business_svc.get_info.return_value = _info(time=0, enabled=False, online=False)
    mixin._cb_info_user(7, "alice")
    text = telegram.send_message.call_args.args[1]
    assert "N/A" in text
    assert "🔴 Отключен" in text

    subscription.business_svc.get_info.side_effect = NotFoundError("missing")
    mixin._cb_info_user(7, "alice")
    assert "missing" in cast(MagicMock, mixin._send_message).call_args.args[1]

    subscription.business_svc.get_info.side_effect = RuntimeError("boom")
    mixin._cb_info_user(7, "alice")
    assert "Внутренняя ошибка" in cast(MagicMock, mixin._send_message).call_args.args[1]
    cast(MagicMock, mixin.log).error.assert_called()


def test_delete_and_edit_options(
    users: tuple[AdminUsersMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = users
    mixin._cb_del_user(7, "alice")
    subscription.business_svc.delete_user.assert_called_once_with(username="alice", perma=True)
    assert "удален" in cast(MagicMock, mixin._send_message).call_args.args[1]

    subscription.business_svc.delete_user.side_effect = AppError("nope")
    mixin._cb_del_user(7, "alice")
    assert "nope" in cast(MagicMock, mixin._send_message).call_args.args[1]

    subscription.business_svc.delete_user.side_effect = RuntimeError("boom")
    mixin._cb_del_user(7, "alice")
    assert "Внутренняя ошибка" in cast(MagicMock, mixin._send_message).call_args.args[1]

    mixin._cb_edit_user_options(7, "alice")
    assert mixin._pending_edits[7] == {"username": "alice"}
    assert "edit_fp" in _buttons(telegram.send_message.call_args.kwargs["reply_markup"])


def test_edit_prompts_and_lookup_errors(
    users: tuple[AdminUsersMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = users
    subscription.business_svc.get_info.return_value = _info()
    mixin._cb_edit_fingerprint(7, "alice")
    labels = [button.text for row in telegram.send_message.call_args.kwargs["reply_markup"].inline_keyboard for button in row]
    assert "✅ chrome" in labels

    mixin._cb_edit_limit(7, "alice")
    telegram.register_next_step_handler.assert_called()
    assert mixin._step_edit_limit in telegram.register_next_step_handler.call_args.args

    mixin._cb_edit_wl_limit(7, "alice")
    mixin._cb_edit_time(7, "alice")
    mixin._cb_edit_name(7, "alice")
    assert telegram.register_next_step_handler.call_count == 4

    subscription.business_svc.get_info.side_effect = NotFoundError("gone")
    for method in (
        mixin._cb_edit_fingerprint,
        mixin._cb_edit_limit,
        mixin._cb_edit_wl_limit,
        mixin._cb_edit_time,
        mixin._cb_edit_name,
    ):
        method(7, "alice")
        assert "gone" in telegram.send_message.call_args.args[1]


def test_edit_steps_validate_and_update(
    users: tuple[AdminUsersMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = users
    mixin._pending_edits[7] = {"username": "alice"}
    mixin._step_edit_limit(_message("/start"), "alice")
    telegram.send_message.assert_not_called()

    mixin._step_edit_limit(_message("nope"), "alice")
    assert "число" in telegram.send_message.call_args.args[1]

    subscription.business_svc.update_params.side_effect = AppError("bad")
    mixin._step_edit_limit(_message(" 8 "), "alice")
    assert "bad" in telegram.send_message.call_args.args[1]

    subscription.business_svc.update_params.side_effect = None
    mixin._step_edit_limit(_message("8"), "alice")
    subscription.business_svc.update_params.assert_called_with(username="alice", limit=8)
    assert 7 not in mixin._pending_edits

    mixin._pending_edits[7] = {"username": "alice"}
    mixin._step_edit_wl_limit(_message("/x"), "alice")
    mixin._step_edit_wl_limit(_message("x"), "alice")
    mixin._step_edit_wl_limit(_message("3"), "alice")
    subscription.business_svc.update_params.assert_called_with(username="alice", wl_limit=3)

    mixin._pending_edits[7] = {"username": "alice"}
    mixin._step_edit_time(_message("0"), "alice")
    subscription.business_svc.update_params.assert_called_with(username="alice", timee=0)
    assert "безлимит" in telegram.send_message.call_args.args[1]

    mixin._step_edit_time(_message("2"), "alice")
    assert "дней" in telegram.send_message.call_args.args[1]
    mixin._step_edit_time(_message("bad"), "alice")
    mixin._step_edit_time(_message("/cancel"), "alice")

    mixin._pending_edits[7] = {"username": "alice"}
    mixin._step_edit_name(_message("x" * 17), "alice")
    assert "длинное" in telegram.send_message.call_args.args[1]
    mixin._step_edit_name(_message("  Bob  "), "alice")
    subscription.business_svc.update_params.assert_called_with(username="alice", displayname="Bob")
    mixin._step_edit_name(_message("/skip"), "alice")


def test_reset_and_add_user_flow(
    users: tuple[AdminUsersMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = users
    mixin._step_reset_user(_message("/start"))
    telegram.send_message.assert_not_called()

    subscription.business_svc.reset_user.side_effect = NotFoundError("missing")
    mixin._step_reset_user(_message("alice"))
    assert "missing" in telegram.send_message.call_args.args[1]

    subscription.business_svc.reset_user.side_effect = None
    subscription.business_svc.reset_user.return_value = SimpleNamespace(token="tok", uuid="uid")
    mixin._step_reset_user(_message(" alice "))
    assert "tok" in telegram.send_message.call_args.args[1]

    subscription.user_svc.isuser.return_value = True
    mixin._step_add_user_name(_message("alice"))
    assert "уже существует" in telegram.send_message.call_args.args[1]

    subscription.user_svc.isuser.return_value = False
    mixin._step_add_user_name(_message(" bob "))
    mixin._step_add_user_display(_message("Bob"), "bob")
    mixin._step_add_user_limit(_message("nope"), "bob", "Bob")
    assert "числом" in telegram.send_message.call_args.args[1]
    mixin._step_add_user_limit(_message("5"), "bob", "Bob")
    mixin._step_add_user_time(_message("bad"), "bob", "Bob", 5)
    mixin._step_add_user_time(_message("0"), "bob", "Bob", 5)
    subscription.business_svc.add_new_user.assert_called_with(
        username="bob", displayname="Bob", limit=5, timee=0,
    )

    subscription.business_svc.add_new_user.side_effect = AppError("denied")
    mixin._step_add_user_time(_message("1"), "bob", "Bob", 5)
    assert "denied" in cast(MagicMock, mixin._send_message).call_args.args[1]

    subscription.business_svc.add_new_user.side_effect = RuntimeError("boom")
    mixin._step_add_user_time(_message("1"), "bob", "Bob", 5)
    assert "Внутренняя ошибка" in cast(MagicMock, mixin._send_message).call_args.args[1]

    mixin._step_add_user_name(_message("/start"))
    mixin._step_add_user_display(_message("/start"), "bob")
    mixin._step_add_user_limit(_message("/start"), "bob", "Bob")
    mixin._step_add_user_time(_message("/start"), "bob", "Bob", 5)


def test_info_step_forwards_username(
    users: tuple[AdminUsersMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = users
    subscription.business_svc.get_info.return_value = _info(time=0)
    mixin._step_info_user(_message("/menu"))
    telegram.send_message.assert_not_called()
    mixin._step_info_user(_message(" alice "))
    subscription.business_svc.get_info.assert_called_with("alice", pretty=True)
