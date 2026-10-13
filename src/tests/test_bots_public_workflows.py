from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bot_helpers import subscription_mock, telegram_mock
from helpers import config_mock, language_config, profile_config, subscription_config
from telebot import types

from bots.public.account import PublicAccountMixin
from bots.public.login import PublicLoginMixin
from bots.public.settings import PublicSettingsMixin
from bots.public.traffic import PublicTrafficMixin
from errors import AppError, NotFoundError

TEXTS = {
    "en": {
        "settings_name_prompt": "name?",
        "settings_fp_prompt": "fp?",
        "settings_login_prompt": "login?",
        "settings_pass_prompt": "pass?",
        "settings_fp_success": "fp ok",
        "settings_name_success": "name ok",
        "settings_login_success": "login ok",
        "settings_pass_success": "pass ok",
        "length_displayname": "name>{ln}",
        "length_username": "login>{ln}",
        "no_account": "no login",
        "delete_confirm_input": "DELETE",
        "delete_success": "deleted",
        "reset_confirm_input": "RESET",
        "reset_success": "reset",
        "cancelled": "cancelled",
        "bonus_success": "bonus",
        "invalid_code": "bad code",
        "error_generic": "generic",
        "enter_email": "email?",
        "enter_pass": "password?",
        "login_success": "in",
        "login_fail": "out",
        "chart_invalid_period": "bad period",
        "chart_generating": "working",
        "chart_text": "{days}:{used}/{limit}:{percent}:{wl_used}/{wl_limit}:{wl_percent}",
        "unlimited": "Unlimited",
        "no_data": "empty",
        "get_sub_text": "link {link}",
        "get_sub_btn_link": "open",
        "get_sub_btn_happ": "happ",
        "btn_login": "Login",
        "btn_main_account": "Account",
        "btn_main_sub": "Sub",
        "btn_lang": "Lang",
        "btn_support": "Support",
        "welcome_reg": "hi reg",
        "welcome_new": "hi new",
        "lang_set": "set",
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
    },
    "ru": {"btn_login": "Вход"},
}


def _call(data: str, user_id: int = 42) -> types.CallbackQuery:
    return cast(
        types.CallbackQuery,
        SimpleNamespace(
            id="cb",
            data=data,
            from_user=SimpleNamespace(id=user_id),
            message=SimpleNamespace(message_id=9, chat=SimpleNamespace(id=7)),
        ),
    )


def _message(text: str, user_id: int = 42) -> types.Message:
    return cast(
        types.Message,
        SimpleNamespace(
            text=text,
            from_user=SimpleNamespace(id=user_id),
            chat=SimpleNamespace(id=7),
            message_id=5,
        ),
    )


def _wire[M: (PublicSettingsMixin, PublicAccountMixin, PublicLoginMixin, PublicTrafficMixin)](
    mixin: M,
) -> tuple[M, MagicMock, MagicMock]:
    telegram = telegram_mock()
    subscription = subscription_mock()
    setattr(mixin, "_expect", MagicMock())
    setattr(mixin, "_clear_steps", MagicMock())
    mixin.bot = telegram
    mixin.sub = subscription
    mixin.log = MagicMock()
    mixin.TEXTS = TEXTS
    mixin.cfg = config_mock(
        subscription_config(
            fingerprints=["chrome", "firefox"],
            uri="/sub/",
            domain="https://example.test/",
            profiles={
                "fast": profile_config(
                    name=["Fast", "Быстрый"],
                    description=["fast en", "fast ru"],
                ),
            },
        ),
    )
    setattr(mixin, "get_lang", AsyncMock(return_value="en"))
    setattr(mixin, "get_menu", AsyncMock(return_value="menu"))
    setattr(mixin, "cmd_start", AsyncMock())
    setattr(mixin, "send_info", AsyncMock())
    setattr(mixin, "_answer_callback", AsyncMock())
    setattr(mixin, "_delete_message", AsyncMock())
    setattr(mixin, "_send_message", AsyncMock())
    telegram.send_message.return_value = _message("next")
    return mixin, telegram, subscription


@pytest.fixture
def settings() -> tuple[PublicSettingsMixin, MagicMock, MagicMock]:
    return _wire(PublicSettingsMixin.__new__(PublicSettingsMixin))


@pytest.mark.asyncio
async def test_settings_callback_requires_registration_and_dispatches(
    settings: tuple[PublicSettingsMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = settings
    subscription.telegram_svc.is_registered.return_value = False
    await mixin.settings_callback(_call("set_name"))
    telegram.send_message.assert_not_called()

    subscription.telegram_svc.is_registered.return_value = True
    subscription.telegram_svc.get_username_telegram.return_value = "alice"
    subscription.user_svc.get_fingerprint.return_value = "chrome"
    await mixin.settings_callback(_call("set_fp"))
    labels = [
        button.text
        for row in telegram.send_message.call_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "✅ chrome" in labels
    cast(MagicMock, mixin._delete_message).assert_called()

    for action, step in (
        ("set_name", mixin.step_settings_name),
        ("set_login", mixin.step_settings_login),
        ("set_pass", mixin.step_settings_pass),
    ):
        await mixin.settings_callback(_call(action))
        registered = cast(
            Callable[[types.Message], None],
            cast(MagicMock, mixin._expect).call_args.args[1],
        )
        assert getattr(registered, "__func__", registered) is getattr(step, "__func__", step)


@pytest.mark.asyncio
async def test_fingerprint_and_settings_steps(
    settings: tuple[PublicSettingsMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = settings
    subscription.telegram_svc.is_registered.return_value = False
    await mixin.fp_callback(_call("fp_firefox"))
    cast(MagicMock, mixin._answer_callback).assert_not_called()

    subscription.telegram_svc.is_registered.return_value = True
    subscription.telegram_svc.get_username_telegram.return_value = None
    await mixin.fp_callback(_call("fp_firefox"))
    subscription.business_svc.update_params.assert_not_called()

    subscription.telegram_svc.get_username_telegram.return_value = "alice"
    subscription.business_svc.update_params.side_effect = AppError("bad")
    await mixin.fp_callback(_call("fp_firefox"))
    assert "bad" in telegram.send_message.call_args.args[1]
    subscription.business_svc.update_params.side_effect = None
    await mixin.fp_callback(_call("fp_firefox"))
    subscription.business_svc.update_params.assert_called_with(
        username="alice",
        fingerprint="firefox",
    )
    assert "fp ok" in telegram.send_message.call_args.args[1]

    await mixin.step_settings_name(_message("/start"))
    cast(MagicMock, mixin.cmd_start).assert_called()
    subscription.telegram_svc.get_username_telegram.return_value = 1
    await mixin.step_settings_name(_message("Bob"))
    subscription.business_svc.update_params.assert_called_with(
        username="alice",
        fingerprint="firefox",
    )

    subscription.telegram_svc.get_username_telegram.return_value = "alice"
    await mixin.step_settings_name(_message("x" * 17))
    assert "name>16" in telegram.send_message.call_args.args[1]
    await mixin.step_settings_name(_message(" Bob "))
    subscription.business_svc.update_params.assert_called_with(username="alice", displayname="Bob")

    await mixin.step_settings_login(_message("/menu"))
    await mixin.step_settings_login(_message("y" * 33))
    assert "login>16" in telegram.send_message.call_args.args[1]
    subscription.business_svc.update_params.side_effect = AppError("taken")
    await mixin.step_settings_login(_message("bob"))
    assert "taken" in telegram.send_message.call_args.args[1]
    subscription.business_svc.update_params.side_effect = None
    await mixin.step_settings_login(_message("bob"))
    subscription.business_svc.update_params.assert_called_with(username="alice", ext_username="bob")

    await mixin.step_settings_pass(_message("/start"))
    subscription.user_svc.get_external_username.return_value = ""
    await mixin.step_settings_pass(_message("secret"))
    assert "no login" in telegram.send_message.call_args.args[1]

    subscription.user_svc.get_external_username.side_effect = ["bob", ""]
    await mixin.step_settings_pass(_message("secret"))
    assert "No login found" in telegram.send_message.call_args.args[1]
    cast(MagicMock, mixin._delete_message).assert_called_with(7, 5, secret=True)

    subscription.user_svc.get_external_username.side_effect = None
    subscription.user_svc.get_external_username.return_value = "bob"
    subscription.business_svc.update_params.side_effect = AppError("weak")
    await mixin.step_settings_pass(_message("secret"))
    assert "weak" in telegram.send_message.call_args.args[1]
    subscription.business_svc.update_params.side_effect = None
    await mixin.step_settings_pass(_message(" secret "))
    subscription.business_svc.update_params.assert_called_with(
        username="alice",
        ext_username="bob",
        ext_password="secret",
    )
    assert "pass ok" in telegram.send_message.call_args.args[1]


@pytest.fixture
def account() -> tuple[PublicAccountMixin, MagicMock, MagicMock]:
    return _wire(PublicAccountMixin.__new__(PublicAccountMixin))


@pytest.mark.asyncio
async def test_delete_reset_and_bonus(
    account: tuple[PublicAccountMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = account
    await mixin.step_delete(_message("/start"))
    cast(MagicMock, mixin.cmd_start).assert_called()
    await mixin.step_delete(_message("no"))
    assert "cancelled" in telegram.send_message.call_args.args[1]

    subscription.telegram_svc.get_username_telegram.return_value = None
    await mixin.step_delete(_message("delete"))
    assert "Error" in telegram.send_message.call_args.args[1]

    subscription.telegram_svc.get_username_telegram.return_value = "alice"
    subscription.business_svc.delete_user.side_effect = AppError("busy")
    await mixin.step_delete(_message("DELETE"))
    assert "busy" in cast(MagicMock, mixin._send_message).call_args.args[1]
    subscription.business_svc.delete_user.side_effect = RuntimeError("boom")
    await mixin.step_delete(_message("DELETE"))
    assert "Error" in cast(MagicMock, mixin._send_message).call_args.args[1]
    subscription.business_svc.delete_user.side_effect = None
    await mixin.step_delete(_message("DELETE"))
    subscription.business_svc.delete_user.assert_called_with(username="alice", perma=True)
    assert "deleted" in telegram.send_message.call_args.args[1]

    await mixin.step_reset(_message("/start"))
    await mixin.step_reset(_message("nope"))
    assert "cancelled" in telegram.send_message.call_args.args[1]
    subscription.telegram_svc.get_username_telegram.return_value = 1
    await mixin.step_reset(_message("reset"))
    subscription.business_svc.reset_user.assert_not_called()
    subscription.telegram_svc.get_username_telegram.return_value = "alice"
    subscription.business_svc.reset_user.side_effect = NotFoundError("gone")
    await mixin.step_reset(_message("RESET"))
    assert "gone" in cast(MagicMock, mixin._send_message).call_args.args[1]
    subscription.business_svc.reset_user.side_effect = RuntimeError("boom")
    await mixin.step_reset(_message("RESET"))
    assert "generic" in cast(MagicMock, mixin._send_message).call_args.args[1]
    cast(MagicMock, mixin.log).critical.assert_called()
    subscription.business_svc.reset_user.side_effect = None
    await mixin.step_reset(_message("RESET"))
    assert "reset" in telegram.send_message.call_args.args[1]

    await mixin.step_bonus(_message("/cancel"))
    assert "cancelled" in telegram.send_message.call_args.args[1]
    subscription.telegram_svc.bonus_code.side_effect = AppError("bad")
    await mixin.step_bonus(_message("CODE"))
    assert "bad code" in cast(MagicMock, mixin._send_message).call_args.args[1]
    subscription.telegram_svc.bonus_code.side_effect = RuntimeError("boom")
    await mixin.step_bonus(_message("CODE"))
    assert "generic" in cast(MagicMock, mixin._send_message).call_args.args[1]
    subscription.telegram_svc.bonus_code.side_effect = None
    await mixin.step_bonus(_message(" CODE "))
    subscription.telegram_svc.bonus_code.assert_called_with(value=42, code="CODE")
    cast(MagicMock, mixin.send_info).assert_called_with(7, 42, "en")


@pytest.fixture
def login() -> tuple[PublicLoginMixin, MagicMock, MagicMock]:
    return _wire(PublicLoginMixin.__new__(PublicLoginMixin))


@pytest.mark.asyncio
async def test_login_flow(
    login: tuple[PublicLoginMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = login
    subscription.telegram_svc.is_registered.return_value = True
    await mixin.login_callback(_call("login_credentials"))
    telegram.send_message.assert_not_called()

    subscription.telegram_svc.is_registered.return_value = False
    await mixin.login_callback(_call("login_other"))
    cast(MagicMock, mixin._answer_callback).assert_called_once()
    await mixin.login_callback(_call("login_credentials"))
    registered = cast(
        Callable[[types.Message], None],
        cast(MagicMock, mixin._expect).call_args.args[1],
    )
    assert getattr(registered, "__func__", registered) is getattr(
        mixin.step_login_email,
        "__func__",
        mixin.step_login_email,
    )

    await mixin.step_login_email(_message("/start"))
    cast(MagicMock, mixin.cmd_start).assert_called()
    await mixin.step_login_email(_message(" bob@example.test "))
    assert cast(MagicMock, mixin._expect).call_args.args[2] == "bob@example.test"

    await mixin.step_login_pass(_message("/start"), "bob@example.test")
    subscription.password_svc.validate_credentials.return_value = ""
    await mixin.step_login_pass(_message(" secret "), "bob@example.test")
    assert "out" in telegram.send_message.call_args.args[1]
    cast(MagicMock, mixin._delete_message).assert_called_with(7, 5, secret=True)

    subscription.password_svc.validate_credentials.return_value = "bob"
    await mixin.step_login_pass(_message("secret"), "bob@example.test")
    subscription.telegram_svc.set_telegram_user.assert_called_with(42, "bob")
    cast(MagicMock, mixin.send_info).assert_called_with(7, 42, "en")
    assert "in" in telegram.send_message.call_args.args[1]


@pytest.fixture
def traffic() -> tuple[PublicTrafficMixin, MagicMock, MagicMock]:
    mixin, telegram, subscription = _wire(PublicTrafficMixin.__new__(PublicTrafficMixin))
    language = language_config()
    language["chart"] = {"en": {"bandwidth": "bw"}}
    mixin.lang_cfg = config_mock(language)
    return mixin, telegram, subscription


@pytest.mark.asyncio
async def test_public_chart_submission_and_render(
    traffic: tuple[PublicTrafficMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = traffic
    subscription.telegram_svc.is_registered.return_value = False
    await mixin.chart_callback(_call("chart_7"))
    cast(MagicMock, mixin._answer_callback).assert_not_called()

    subscription.telegram_svc.is_registered.return_value = True
    await mixin.chart_callback(_call("chart_bad"))
    await mixin.chart_callback(_call("chart_0"))
    cast(MagicMock, mixin._answer_callback).assert_called_with("cb", "bad period")

    subscription.telegram_svc.get_username_telegram.return_value = None
    await mixin.chart_callback(_call("chart_7"))

    subscription.telegram_svc.get_username_telegram.return_value = "alice"
    with patch.object(PublicTrafficMixin, "_render_chart") as render:
        await mixin.chart_callback(_call("chart_14"))
    render.assert_awaited_once_with(uid=42, username="alice", days=14, lang="en", chat_id=7)

    info = SimpleNamespace(
        displayname="Alice",
        bandwidth=SimpleNamespace(
            total=SimpleNamespace(upload=0, download=0),
            wl_total=SimpleNamespace(upload=0, download=0),
            monthly=0,
            limit=0,
            wl_monthly=10,
            wl_limit=1,
        ),
    )
    subscription.bandwidth_svc.get_bw_history.return_value = []
    subscription.business_svc.get_info.return_value = info
    with patch("bots.public.traffic.bandwidth_chart", return_value=b"png"):
        await mixin._render_chart(uid=42, username="alice", days=3, lang="en", chat_id=7)
    telegram.send_photo.assert_called_once()
    assert "Unlimited" in telegram.send_photo.call_args.kwargs["caption"]

    with patch("bots.public.traffic.bandwidth_chart", return_value=None):
        await mixin._render_chart(uid=42, username="alice", days=3, lang="en", chat_id=7)
    assert "empty" in telegram.send_message.call_args.args[1]

    subscription.business_svc.get_info.side_effect = AppError("denied")
    await mixin._render_chart(uid=42, username="alice", days=3, lang="en", chat_id=7)
    assert "denied" in cast(MagicMock, mixin._send_message).call_args.args[1]
    subscription.business_svc.get_info.side_effect = RuntimeError("boom")
    await mixin._render_chart(uid=42, username="alice", days=3, lang="en", chat_id=7)
    assert "generic" in cast(MagicMock, mixin._send_message).call_args.args[1]
