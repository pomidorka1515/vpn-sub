from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from bot_helpers import subscription_mock, telegram_mock
from helpers import config_mock, profile_config, subscription_config
from telebot import types

from bots.telegram.public.common import PublicCommonMixin
from bots.telegram.public.subscription import PublicSubscriptionMixin
from bots.telegram.public.text_routing import PublicTextRoutingMixin


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
    telegram = telegram_mock()
    subscription = subscription_mock()
    setattr(mixin, "_expect", MagicMock())
    setattr(mixin, "_clear_steps", MagicMock())
    mixin.bot = telegram
    mixin.sub = subscription
    mixin.log = MagicMock()
    mixin.TEXTS = TEXTS
    mixin._answer_callback = AsyncMock()  # type: ignore[method-assign]
    mixin._delete_message = AsyncMock()  # type: ignore[method-assign]
    return mixin, telegram, subscription


@pytest.mark.asyncio
async def test_language_menu_and_notifications(
    common: tuple[PublicCommonMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = common
    subscription.telegram_svc.get_telegram_language.return_value = "en"
    assert await mixin.get_lang(42) == "en"
    await mixin.set_lang(42, "ru")
    subscription.telegram_svc.set_telegram_language.assert_called_once_with(42, "ru")

    subscription.telegram_svc.is_registered.return_value = False
    guest = await mixin.get_menu(42)
    labels = [_button_text(button) for row in guest.keyboard for button in row]
    assert labels == ["Login", "Lang", "Support"]

    subscription.telegram_svc.is_registered.return_value = True
    member = await mixin.get_menu(42)
    labels = [_button_text(button) for row in member.keyboard for button in row]
    assert labels[:2] == ["Account", "Sub"]

    subscription.telegram_svc.has_telegram_language.return_value = False
    await mixin.cmd_start(_message())
    cast(MagicMock, mixin._clear_steps).assert_called_once_with(7)
    assert "Welcome" in telegram.send_message.call_args.args[1]

    subscription.telegram_svc.has_telegram_language.return_value = True
    subscription.telegram_svc.is_registered.return_value = False
    await mixin.cmd_start(_message())
    assert telegram.send_message.call_args.args[1] == "hi new"
    subscription.telegram_svc.is_registered.return_value = True
    await mixin.cmd_start(_message())
    assert telegram.send_message.call_args.args[1] == "hi reg"

    await mixin.set_lang_callback(_call("lang_ru"))
    subscription.telegram_svc.set_telegram_language.assert_called_with(42, "ru")
    cast(MagicMock, mixin._delete_message).assert_called_once_with(7, 9)


@pytest.fixture
def routing() -> tuple[PublicTextRoutingMixin, MagicMock, MagicMock]:
    mixin = PublicTextRoutingMixin.__new__(PublicTextRoutingMixin)
    telegram = telegram_mock()
    subscription = subscription_mock()
    setattr(mixin, "_expect", MagicMock())
    setattr(mixin, "_clear_steps", MagicMock())
    mixin.bot = telegram
    mixin.sub = subscription
    mixin.TEXTS = TEXTS
    mixin.cfg = config_mock(
        subscription_config(
            profiles={
                "fast": profile_config(
                    name=["Fast", "Быстрый"],
                    description=["fast en", "fast ru"],
                ),
                "broken": profile_config(
                    name=["Broken", "Сломан"],
                    description=["broken en", "broken ru"],
                ),
            },
        ),
    )
    mixin.get_lang = AsyncMock(return_value="en")  # type: ignore[method-assign]
    mixin.get_menu = AsyncMock(return_value="menu")  # type: ignore[method-assign]
    mixin.send_info = AsyncMock()  # type: ignore[method-assign]
    mixin.send_link = AsyncMock()  # type: ignore[method-assign]
    mixin.step_bonus = AsyncMock()  # type: ignore[method-assign]
    mixin.step_reset = AsyncMock()  # type: ignore[method-assign]
    mixin.step_delete = AsyncMock()  # type: ignore[method-assign]
    telegram.send_message.return_value = _message("next")
    return mixin, telegram, subscription


def _labels(markup: object) -> list[str]:
    keyboard = cast(types.ReplyKeyboardMarkup, markup).keyboard
    return [
        button["text"] if isinstance(button, dict) else button.text
        for row in keyboard
        for button in row
    ]


@pytest.mark.asyncio
async def test_text_handlers_build_menus_and_prompts(
    routing: tuple[PublicTextRoutingMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = routing
    message = _message("Info")
    await mixin._handle_info(message, 42, "en")
    cast(MagicMock, mixin.send_info).assert_called_once_with(7, 42, "en")

    await mixin._handle_bonus(message, 42, "en")
    assert cast(MagicMock, mixin._expect).call_args.args[1] is mixin.step_bonus
    await mixin._handle_language(message, 42, "en")
    assert "lang_en" in [
        button.callback_data
        for row in telegram.send_message.call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]

    subscription.telegram_svc.is_registered.return_value = True
    await mixin._handle_login(message, 42, "en")
    telegram.send_message.reset_mock()
    subscription.telegram_svc.is_registered.return_value = False
    await mixin._handle_login(message, 42, "en")
    assert telegram.send_message.call_args.args[1] == "how"

    await mixin._handle_subscription_menu(message, 42, "en")
    assert "Get" in _labels(telegram.send_message.call_args.kwargs["reply_markup"])
    await mixin._handle_account_menu(message, 42, "en")
    assert "Delete" in _labels(telegram.send_message.call_args.kwargs["reply_markup"])
    await mixin._handle_main_menu(message, 42, "en")
    assert telegram.send_message.call_args.kwargs["reply_markup"] == "menu"

    await mixin._handle_reset(message, 42, "en")
    assert cast(MagicMock, mixin._expect).call_args.args[1] is mixin.step_reset
    await mixin._handle_support(message, 42, "en")
    assert telegram.send_message.call_args.kwargs["parse_mode"] == "HTML"
    await mixin._handle_logout(message, 42, "en")
    subscription.telegram_svc.set_telegram_user.assert_called_once_with(42, None)

    await mixin._handle_help(message, 42, "en")
    assert "<code>Fast</code> — fast en" in telegram.send_message.call_args.args[1]
    await mixin._handle_settings(message, 42, "en")
    assert "set_pass" in [
        button.callback_data
        for row in telegram.send_message.call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    await mixin._handle_delete(message, 42, "en")
    assert cast(MagicMock, mixin._expect).call_args.args[1] is mixin.step_delete
    await mixin._handle_get_subscription(message, 42, "en")
    cast(MagicMock, mixin.send_link).assert_called_once_with(7, 42, "en")
    await mixin._handle_chart(message, 42, "en")
    assert [
        button.callback_data
        for row in telegram.send_message.call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ] == ["chart_3", "chart_14", "chart_30", "chart_90"]


@pytest.mark.asyncio
async def test_send_link_builds_qr_and_happ_redirect() -> None:
    mixin = PublicSubscriptionMixin.__new__(PublicSubscriptionMixin)
    telegram = telegram_mock()
    subscription = subscription_mock()
    setattr(mixin, "_expect", MagicMock())
    setattr(mixin, "_clear_steps", MagicMock())
    mixin.bot = telegram
    mixin.sub = subscription
    mixin.TEXTS = TEXTS
    mixin.cfg = config_mock(subscription_config(uri="/sub/", domain="https://example.test/"))
    subscription.telegram_svc.get_info_telegram.return_value = None
    await mixin.send_info(7, 42, "en")
    await mixin.send_link(7, 42, "en")
    telegram.send_photo.assert_not_called()

    subscription.telegram_svc.get_info_telegram.return_value = SimpleNamespace(token="tok")

    def qr(link: str) -> bytes:
        return b"qr:" + link.encode()

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "bots.telegram.public.subscription.make_qr",
            qr,
        )
        await mixin.send_link(7, 42, "en")
    photo = telegram.send_photo.call_args.args[1]
    assert b"token=tok&lang=en" in photo
    buttons = telegram.send_photo.call_args.kwargs["reply_markup"].inline_keyboard
    urls = [button.url for row in buttons for button in row]
    assert urls[0].endswith("/sub?token=tok&lang=en")
    assert "prefix=happ%3A//add/" in urls[1]
