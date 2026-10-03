from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from telebot import types

from bots.admin.codes import AdminCodesMixin
from custom_types import CodeObject
from errors import AppError, NotFoundError


def _message(text: str, chat_id: int = 7) -> types.Message:
    return cast(
        types.Message,
        SimpleNamespace(text=text, chat=SimpleNamespace(id=chat_id), message_id=3),
    )


def _real_message(text: str, chat_id: int = 7) -> types.Message:
    parsed = cast(types.Message, types.Message.de_json({  # type: ignore[no-untyped-call]
        "message_id": 3,
        "date": 0,
        "chat": {"id": chat_id, "type": "private"},
        "text": text,
    }))
    return parsed


def _code(**overrides: Any) -> CodeObject:
    payload: dict[str, Any] = {
        "code": "WELCOME",
        "action": "register",
        "perma": False,
        "uses": 2,
        "days": 30,
        "gb": 10,
        "wl_gb": 1,
    }
    payload.update(overrides)
    return CodeObject(**payload)


@pytest.fixture
def codes() -> tuple[AdminCodesMixin, MagicMock, MagicMock]:
    mixin = AdminCodesMixin.__new__(AdminCodesMixin)
    telegram = MagicMock()
    subscription = MagicMock()
    mixin.bot = cast(Any, telegram)
    mixin.sub = cast(Any, subscription)
    mixin.log = MagicMock()
    mixin._pending_codes = {}
    mixin.get_codes_menu = MagicMock(return_value="codes")  # type: ignore[method-assign]
    mixin._send_message = MagicMock()  # type: ignore[method-assign]
    telegram.send_message.return_value = _message("next")
    return mixin, telegram, subscription


def test_list_and_info_codes(
    codes: tuple[AdminCodesMixin, MagicMock, MagicMock],
) -> None:
    mixin, _telegram, subscription = codes
    subscription.code_svc.list_code.return_value = []
    mixin._cb_list_codes(7)
    assert "пуст" in cast(MagicMock, mixin._send_message).call_args.args[1]

    subscription.code_svc.list_code.return_value = ["WELCOME", "BONUS"]
    mixin._cb_list_codes(7)
    assert "<code>BONUS</code>" in cast(MagicMock, mixin._send_message).call_args.args[1]

    subscription.code_svc.list_code.side_effect = RuntimeError("boom")
    mixin._cb_list_codes(7)
    assert "Внутренняя ошибка" in cast(MagicMock, mixin._send_message).call_args.args[1]
    cast(MagicMock, mixin.log).error.assert_called()

    mixin._step_info_code(_message("/start"))
    subscription.code_svc.get_code.assert_not_called()

    subscription.code_svc.get_code.side_effect = None
    subscription.code_svc.get_code.return_value = _code(perma=True)
    mixin._step_info_code(_message(" WELCOME "))
    text = cast(MagicMock, mixin._send_message).call_args.args[1]
    assert "register" in text
    assert "Да" in text

    subscription.code_svc.get_code.side_effect = NotFoundError("missing")
    mixin._step_info_code(_message("GONE"))
    assert "missing" in cast(MagicMock, mixin._send_message).call_args.args[1]

    subscription.code_svc.get_code.side_effect = RuntimeError("boom")
    mixin._step_info_code(_message("GONE"))
    assert "Внутренняя ошибка" in cast(MagicMock, mixin._send_message).call_args.args[1]


def test_delete_code_and_type_prompt(
    codes: tuple[AdminCodesMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = codes
    mixin._step_del_code(_message("/cancel"))
    subscription.code_svc.delete_code.assert_not_called()

    subscription.code_svc.delete_code.side_effect = AppError("nope")
    mixin._step_del_code(_message("WELCOME"))
    assert "nope" in cast(MagicMock, mixin._send_message).call_args.args[1]

    subscription.code_svc.delete_code.side_effect = RuntimeError("boom")
    mixin._step_del_code(_message("WELCOME"))
    assert "Внутренняя ошибка" in cast(MagicMock, mixin._send_message).call_args.args[1]

    subscription.code_svc.delete_code.side_effect = None
    mixin._step_del_code(_message(" WELCOME "))
    subscription.code_svc.delete_code.assert_called_with("WELCOME")
    assert "удалён" in cast(MagicMock, mixin._send_message).call_args.args[1]

    mixin._step_add_code_name(_message("/start"))
    assert mixin._pending_codes == {}
    mixin._step_add_code_name(_message(" WELCOME "))
    assert mixin._pending_codes[7] == "WELCOME"
    buttons = [
        button.callback_data
        for row in telegram.send_message.call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert buttons == ["codetype_register", "codetype_bonus", "codes_menu"]


def test_add_code_wizard_rejects_bad_numbers_and_creates_code(
    codes: tuple[AdminCodesMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = codes
    mixin._step_add_code_days(_message("/x"), "register", "WELCOME")
    mixin._step_add_code_days(_message("days"), "register", "WELCOME")
    assert "число" in telegram.send_message.call_args.args[1]

    mixin._step_add_code_days(_message("30"), "register", "WELCOME")
    assert mixin._step_add_code_time in telegram.register_next_step_handler.call_args.args

    mixin._step_add_code_time(_message("gb"), "register", "WELCOME", 30)
    mixin._step_add_code_time(_message("10"), "register", "WELCOME", 30)
    mixin._step_add_code_wl_time(_message("wl"), "register", "WELCOME", 30, 10)
    mixin._step_add_code_wl_time(_message("1"), "register", "WELCOME", 30, 10)
    mixin._step_add_code_perma(_message("maybe"), "register", "WELCOME", 30, 10, 1)
    assert "да/нет" in telegram.send_message.call_args.args[1]

    mixin._step_add_code_perma(_message("Нет"), "register", "WELCOME", 30, 10, 1)
    assert mixin._step_add_code_uses in telegram.register_next_step_handler.call_args.args

    mixin._step_add_code_uses(_real_message("/start"), "register", "WELCOME", 30, 10, 1, False)
    telegram.send_message.reset_mock()
    mixin._step_add_code_uses(_real_message("0"), "register", "WELCOME", 30, 10, 1, False)
    assert "больше 0" in telegram.send_message.call_args.args[1]

    mixin._step_add_code_uses(_real_message("2"), "bonus", "WELCOME", 30, 10, 1, False)
    subscription.code_svc.add_code.assert_called_with(
        code="WELCOME", action="bonus", permanent=False,
        days=30, gb=10, wl_gb=1, uses=2,
    )
    created = cast(MagicMock, mixin._send_message).call_args.args[1]
    assert "Код создан" in created
    assert "<code>2</code>" in created

    subscription.code_svc.add_code.side_effect = AppError("duplicate")
    mixin._step_add_code_perma(_message("да"), "register", "FOREVER", 0, 0, 0)
    subscription.code_svc.add_code.assert_called_with(
        code="FOREVER", action="register", permanent=True,
        days=0, gb=0, wl_gb=0, uses=-1,
    )
    assert "duplicate" in cast(MagicMock, mixin._send_message).call_args.args[1]

    subscription.code_svc.add_code.side_effect = RuntimeError("boom")
    mixin._step_add_code_uses(7, "register", "FOREVER", 0, 0, 0, True)
    assert "Внутренняя ошибка" in cast(MagicMock, mixin._send_message).call_args.args[1]
