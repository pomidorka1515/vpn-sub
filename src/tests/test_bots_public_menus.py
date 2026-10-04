from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from telebot import types

from bots.public.common import PublicCommonMixin
from bots.public.subscription import PublicSubscriptionMixin
from bots.public.text_routing import PublicTextRoutingMixin


def _button_text(button: types.KeyboardButton | dict[str, str]) -> str:
    if isinstance(button, dict):
        return button["text"]
    return button.text


TEXTS = {
    "en": {
        "btn_login": "Login",
        "btn_main_account": "Account",
        "btn_main_sub": "Sub",
        "btn_lang": "Lang",
        "btn_support": "Support",
        "welcome_reg": "hi reg",
        "welcome_new": "hi new",
        "lang_set": "language set",
        "enter_bonus": "code?",
        "choose_lang": "choose",
        "btn_login_credentials": "creds",
        "choose_login": "how",
        "btn_main_back": "Back",
        "btn_info": "Info",
        "btn_get_sub": "Get",
        "btn_bonus": "Bonus",
        "btn_reset": "Reset",
        "btn_chart": "Chart",
        "btn_settings": "Settings",
        "btn_logout": "Logout",
        "btn_help": "Help",
        "btn_delete": "Delete",
        "confirm_reset": "sure reset",
        "support_text": "helpdesk",
        "logout_success": "bye",
        "help_text": "profiles\n{text}",
        "name_label": "Name",
        "fp_label": "FP",
        "login_label": "Login",
        "pass_label": "Pass",
        "settings_menu": "settings",
        "confirm_delete": "sure delete",
        "btn_chart_days": "{days}d",
        "choose_chart_days": "period",
        "notify": "hello {name}",
        "get_sub_text": "link {link}",
        "get_sub_btn_link": "open",
        "get_sub_btn_happ": "happ",
    },
    "ru": {"btn_login": "Вход", "notify": "привет", "lang_set": "язык выбран"},
}


def _message(text: str = "hi", user_id: int = 42) -> types.Message:
    return cast(
        types.Message,
        SimpleNamespace(
            text=text,
            from_user=SimpleNamespace(id=user_id),
            chat=SimpleNamespace(id=7),
            message_id=5,
        ),
    )


def _call(data: str) -> types.CallbackQuery:
    return cast(
        types.CallbackQuery,
        SimpleNamespace(
            id="cb",
            data=data,
            from_user=SimpleNamespace(id=42),
            message=SimpleNamespace(message_id=9, chat=SimpleNamespace(id=7)),
        ),
    )


@pytest.fixture
def common() -> tuple[PublicCommonMixin, MagicMock, MagicMock]:
    mixin = PublicCommonMixin.__new__(PublicCommonMixin)
    telegram = MagicMock()
    subscription = MagicMock()
    mixin.bot = cast(Any, telegram)
    mixin.sub = cast(Any, subscription)
    mixin.log = MagicMock()
    mixin.TEXTS = TEXTS
    mixin.start_polling = MagicMock()  # type: ignore[method-assign]
    mixin.stop_polling = MagicMock()  # type: ignore[method-assign]
    mixin._executor = MagicMock()
    mixin._answer_callback = MagicMock()  # type: ignore[method-assign]
    mixin._delete_message = MagicMock()  # type: ignore[method-assign]
    return mixin, telegram, subscription


def test_language_menu_and_notifications(
    common: tuple[PublicCommonMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = common
    subscription.telegram_svc.get_telegram_language.return_value = "en"
    assert mixin.get_lang(42) == "en"
    mixin.set_lang(42, "ru")
    subscription.telegram_svc.set_telegram_language.assert_called_once_with(42, "ru")

    subscription.telegram_svc.is_registered.return_value = False
    guest = mixin.get_menu(42)
    labels = [_button_text(button) for row in guest.keyboard for button in row]
    assert labels == ["Login", "Lang", "Support"]

    subscription.telegram_svc.is_registered.return_value = True
    member = mixin.get_menu(42)
    labels = [_button_text(button) for row in member.keyboard for button in row]
    assert labels[:2] == ["Account", "Sub"]

    subscription.telegram_svc.has_telegram_language.return_value = False
    mixin.cmd_start(_message())
    telegram.clear_step_handler_by_chat_id.assert_called_once_with(7)
    assert "Welcome" in telegram.send_message.call_args.args[1]

    subscription.telegram_svc.has_telegram_language.return_value = True
    subscription.telegram_svc.is_registered.return_value = False
    mixin.cmd_start(_message())
    assert telegram.send_message.call_args.args[1] == "hi new"
    subscription.telegram_svc.is_registered.return_value = True
    mixin.cmd_start(_message())
    assert telegram.send_message.call_args.args[1] == "hi reg"

    mixin.set_lang_callback(_call("lang_ru"))
    subscription.telegram_svc.set_telegram_language.assert_called_with(42, "ru")
    cast(MagicMock, mixin._delete_message).assert_called_once_with(7, 9)
    mixin.start()
    mixin.stop()
    cast(MagicMock, mixin.start_polling).assert_called_once()
    cast(MagicMock, mixin._executor.shutdown).assert_called_once_with(wait=False, cancel_futures=True)


@pytest.fixture
def routing() -> tuple[PublicTextRoutingMixin, MagicMock, MagicMock]:
    mixin = PublicTextRoutingMixin.__new__(PublicTextRoutingMixin)
    telegram = MagicMock()
    subscription = MagicMock()
    mixin.bot = cast(Any, telegram)
    mixin.sub = cast(Any, subscription)
    mixin.TEXTS = TEXTS
    mixin.cfg = cast(Any, {
        "profileDescriptions": {"fast": ["fast en", "fast ru"]},
        "profiles": {"fast": ["Fast", "Быстрый"], "broken": ["Broken", "Сломан"]},
    })
    mixin.get_lang = MagicMock(return_value="en")  # type: ignore[method-assign]
    mixin.get_menu = MagicMock(return_value="menu")  # type: ignore[method-assign]
    mixin.send_info = MagicMock()  # type: ignore[method-assign]
    mixin.send_link = MagicMock()  # type: ignore[method-assign]
    mixin.step_bonus = MagicMock()  # type: ignore[method-assign]
    mixin.step_reset = MagicMock()  # type: ignore[method-assign]
    mixin.step_delete = MagicMock()  # type: ignore[method-assign]
    telegram.send_message.return_value = _message("next")
    return mixin, telegram, subscription


def _labels(markup: object) -> list[str]:
    keyboard = cast(Any, markup).keyboard
    return [
        button["text"] if isinstance(button, dict) else button.text
        for row in keyboard
        for button in row
    ]


def test_text_handlers_build_menus_and_prompts(
    routing: tuple[PublicTextRoutingMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = routing
    message = _message("Info")
    mixin._handle_info(message, 42, "en")
    cast(MagicMock, mixin.send_info).assert_called_once_with(7, 42, "en")

    mixin._handle_bonus(message, 42, "en")
    assert telegram.register_next_step_handler.call_args.args[1] is mixin.step_bonus
    mixin._handle_language(message, 42, "en")
    assert "lang_en" in [
        button.callback_data
        for row in telegram.send_message.call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]

    subscription.telegram_svc.is_registered.return_value = True
    mixin._handle_login(message, 42, "en")
    telegram.send_message.reset_mock()
    subscription.telegram_svc.is_registered.return_value = False
    mixin._handle_login(message, 42, "en")
    assert telegram.send_message.call_args.args[1] == "how"

    mixin._handle_subscription_menu(message, 42, "en")
    assert "Get" in _labels(telegram.send_message.call_args.kwargs["reply_markup"])
    mixin._handle_account_menu(message, 42, "en")
    assert "Delete" in _labels(telegram.send_message.call_args.kwargs["reply_markup"])
    mixin._handle_main_menu(message, 42, "en")
    assert telegram.send_message.call_args.kwargs["reply_markup"] == "menu"

    mixin._handle_reset(message, 42, "en")
    assert telegram.register_next_step_handler.call_args.args[1] is mixin.step_reset
    mixin._handle_support(message, 42, "en")
    assert telegram.send_message.call_args.kwargs["parse_mode"] == "HTML"
    mixin._handle_logout(message, 42, "en")
    subscription.telegram_svc.set_telegram_user.assert_called_once_with(42, None)

    mixin._handle_help(message, 42, "en")
    assert "<code>Fast</code> — fast en" in telegram.send_message.call_args.args[1]
    mixin._handle_settings(message, 42, "en")
    assert "set_pass" in [
        button.callback_data
        for row in telegram.send_message.call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    mixin._handle_delete(message, 42, "en")
    assert telegram.register_next_step_handler.call_args.args[1] is mixin.step_delete
    mixin._handle_get_subscription(message, 42, "en")
    cast(MagicMock, mixin.send_link).assert_called_once_with(7, 42, "en")
    mixin._handle_chart(message, 42, "en")
    assert [
        button.callback_data
        for row in telegram.send_message.call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ] == ["chart_3", "chart_14", "chart_30", "chart_90"]


def test_send_link_builds_qr_and_happ_redirect() -> None:
    mixin = PublicSubscriptionMixin.__new__(PublicSubscriptionMixin)
    telegram = MagicMock()
    subscription = MagicMock()
    mixin.bot = cast(Any, telegram)
    mixin.sub = cast(Any, subscription)
    mixin.TEXTS = TEXTS
    mixin.cfg = cast(Any, {"uri": "/sub/", "domain": "https://example.test/"})
    subscription.telegram_svc.get_info_telegram.return_value = None
    mixin.send_info(7, 42, "en")
    mixin.send_link(7, 42, "en")
    telegram.send_photo.assert_not_called()

    subscription.telegram_svc.get_info_telegram.return_value = SimpleNamespace(token="tok")

    def qr(link: str) -> bytes:
        return b"qr:" + link.encode()

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "bots.public.subscription.make_qr",
            qr,
        )
        mixin.send_link(7, 42, "en")
    photo = telegram.send_photo.call_args.args[1]
    assert b"token=tok&lang=en" in photo
    buttons = telegram.send_photo.call_args.kwargs["reply_markup"].inline_keyboard
    urls = [button.url for row in buttons for button in row]
    assert urls[0].endswith("/sub?token=tok&lang=en")
    assert "prefix=happ%3A//add/" in urls[1]
