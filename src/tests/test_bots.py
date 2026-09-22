from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Callable, cast
from unittest.mock import MagicMock, patch

import pytest
from telebot import types

from bots import AdminBot, PublicBot
from bots.polling import TelegramPollingMixin
from bots.public.subscription import PublicSubscriptionMixin
from custom_types import UserInfo, UserInfoBandwidth, UserInfoBandwidthTotal


_STOP_WAIT_SECONDS = 30


class _NullLogger:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warning(self, message: str, *args: object) -> None:
        if args:
            message = message % args
        self.warnings.append(message)


class _Poller:
    def __init__(self, stop_event: threading.Event) -> None:
        self.stop_polling_calls = 0
        self.polling_started = threading.Event()
        self.token: str | None = None
        self._stop_event = stop_event

    def infinity_polling(self, **kwargs: Any) -> None:
        self.polling_started.set()
        self._stop_event.wait(_STOP_WAIT_SECONDS)

    def stop_polling(self) -> None:
        self.stop_polling_calls += 1


class _TestBot(TelegramPollingMixin):
    def __init__(self, stop_event: threading.Event) -> None:
        self.poller = _Poller(stop_event)
        self.logger = _NullLogger()
        self.bot = cast(Any, self.poller)
        self.log = cast(Any, self.logger)


@pytest.fixture
def stop_event() -> threading.Event:
    return threading.Event()


@pytest.fixture
def polling_bot(stop_event: threading.Event) -> _TestBot:
    return _TestBot(stop_event)


def test_start_polling_reuses_running_thread(
    polling_bot: _TestBot, stop_event: threading.Event,
) -> None:
    polling_bot.start_polling()
    assert polling_bot.polling_thread is not None
    thread = polling_bot.polling_thread
    assert polling_bot.poller.polling_started.wait(1)

    polling_bot.start_polling()
    assert polling_bot.polling_thread is thread

    stop_event.set()
    polling_bot.stop_polling()
    assert not thread.is_alive()
    assert polling_bot.logger.errors == []


def test_stop_polling_is_idempotent_and_does_not_join_without_thread(
    polling_bot: _TestBot, stop_event: threading.Event,
) -> None:
    started = time.monotonic()
    polling_bot.stop_polling()
    assert time.monotonic() - started < 0.1
    assert polling_bot.poller.stop_polling_calls == 1

    polling_bot.start_polling()
    assert polling_bot.poller.polling_started.wait(1)
    stop_event.set()
    polling_bot.stop_polling()
    polling_bot.stop_polling()
    assert polling_bot.poller.stop_polling_calls >= 3


def test_stop_polling_logs_timeout(
    polling_bot: _TestBot, stop_event: threading.Event,
) -> None:
    polling_bot.start_polling()
    assert polling_bot.polling_thread is not None
    thread = polling_bot.polling_thread
    assert polling_bot.poller.polling_started.wait(1)

    polling_bot.stop_polling()
    assert thread.is_alive()
    assert len(polling_bot.logger.errors) == 1

    stop_event.set()
    thread.join(_STOP_WAIT_SECONDS)
    assert not thread.is_alive()


def test_polling_errors_are_logged_without_token_or_traceback(
    polling_bot: _TestBot, stop_event: threading.Event,
) -> None:
    token = "123456:secret-token-value"
    polling_bot.poller.token = token
    raised = threading.Event()

    def infinity_polling(**kwargs: Any) -> None:
        if not raised.is_set():
            raised.set()
            raise ConnectionError(
                f"HTTPSConnectionPool(host='api.telegram.org', port=443): "
                f"Max retries exceeded with url: /bot{token}/getUpdates "
                f"(Caused by ProtocolError('Connection aborted.', "
                f"ConnectionResetError(104, 'Connection reset by peer')))"
            )
        stop_event.wait(_STOP_WAIT_SECONDS)

    polling_bot.poller.infinity_polling = infinity_polling  # type: ignore[method-assign]
    polling_bot.start_polling()
    assert raised.wait(1)
    deadline = time.monotonic() + 1
    while not polling_bot.logger.warnings and time.monotonic() < deadline:
        time.sleep(0.01)

    assert len(polling_bot.logger.warnings) == 1
    warning = polling_bot.logger.warnings[0]
    assert "telegram polling failed:" in warning
    assert "ConnectionError" in warning
    assert token not in warning
    assert "secret-token-value" not in warning
    assert "Traceback" not in warning
    assert polling_bot.logger.errors == []

    stop_event.set()
    polling_bot.stop_polling()


def _public_message(text: str) -> types.Message:
    return cast(
        types.Message,
        SimpleNamespace(text=text, from_user=SimpleNamespace(id=42), chat=SimpleNamespace(id=7)),
    )


@pytest.fixture
def public_bot() -> tuple[PublicBot, MagicMock, MagicMock]:
    bot = PublicBot.__new__(PublicBot)
    telegram = MagicMock()
    subscription = MagicMock()
    bot.bot = cast(Any, telegram)
    bot.sub = cast(Any, subscription)
    subscription.telegram_svc.get_telegram_language.return_value = "en"
    bot.TEXTS = {
        lang: {button: f"{lang}:{button}" for button, _, _ in bot.ROUTES}
        for lang in ("ru", "en")
    }
    return bot, telegram, subscription


@pytest.mark.parametrize(
    ("button", "handler_name", "lang"),
    [
        (button, handler_name, lang)
        for button, handler_name, _requires_reg in PublicBot.ROUTES
        for lang in ("ru", "en")
    ],
)
def test_routes_both_languages_to_first_matching_handler(
    public_bot: tuple[PublicBot, MagicMock, MagicMock],
    button: str, handler_name: str, lang: str,
) -> None:
    bot, _telegram, _subscription = public_bot
    message = _public_message(bot.TEXTS[lang][button])
    with patch.object(PublicBot, handler_name) as handler:
        bot.handle_text(message)
    handler.assert_called_once_with(message, 42, "en")


def test_unknown_text_is_ignored(
    public_bot: tuple[PublicBot, MagicMock, MagicMock],
) -> None:
    bot, telegram, subscription = public_bot
    bot.handle_text(_public_message("not a button"))
    telegram.send_message.assert_not_called()
    subscription.telegram_svc.is_registered.assert_not_called()


def test_chart_buttons_keep_original_order_and_registration_guard(
    public_bot: tuple[PublicBot, MagicMock, MagicMock],
) -> None:
    bot, telegram, subscription = public_bot
    bot.TEXTS["en"].update(
        btn_chart_days="Last {days} days", choose_chart_days="Choose a period"
    )
    message = _public_message(bot.TEXTS["ru"]["btn_chart"])
    subscription.telegram_svc.is_registered.return_value = False
    bot.handle_text(message)
    telegram.send_message.assert_not_called()

    subscription.telegram_svc.is_registered.return_value = True
    bot.handle_text(message)
    args, kwargs = telegram.send_message.call_args
    assert args == (7, "Choose a period")
    markup = kwargs["reply_markup"]
    assert [button.callback_data for row in markup.keyboard for button in row] == [
        "chart_3", "chart_14", "chart_30", "chart_90",
    ]


def test_registered_routes_are_blocked_before_handler_dispatch(
    public_bot: tuple[PublicBot, MagicMock, MagicMock],
) -> None:
    bot, _telegram, subscription = public_bot
    subscription.telegram_svc.is_registered.return_value = False
    message = _public_message(bot.TEXTS["en"]["btn_info"])
    with patch.object(PublicBot, "_handle_info") as handler:
        bot.handle_text(message)
    handler.assert_not_called()
    subscription.telegram_svc.is_registered.assert_called_once_with(42)


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


class _ChartOnlyPublicBot(PublicBot):
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1)
        self.stopped_polling = False

    def stop_polling(self) -> None:
        self.stopped_polling = True

    def submit(self, function: Callable[[], bool]) -> Future[bool]:
        return self._executor.submit(function)

    def close(self) -> None:
        self._executor.shutdown(wait=True)


def test_public_bot_stop_does_not_wait_for_running_chart() -> None:
    bot = _ChartOnlyPublicBot()
    release = threading.Event()
    future = bot.submit(release.wait)
    bot.stop()
    assert bot.stopped_polling
    assert not future.done()
    release.set()
    bot.close()


INFO_TEXTS = {
    "en": {
        "info_text": (
            "ℹ️ <b>Your Profile:</b> <code>{username}</code>\n"
            "Enabled: {status}\n"
            "Access to WL locations: {wl_status}\n"
            "Online: {online}\n\n"
            "📊 <b>Traffic:</b>\n"
            "Total downloaded: {total}\n"
            "This month: {monthly}\n"
            "WL traffic total: {wl_total}\n"
            "WL traffic this month: {wl_monthly}\n"
            "Detailed info:\n"
            "↑ {up} / ↓ {down}\n"
            "WL: ↑ {wl_up} / ↓ {wl_down}\n\n"
            "<i>WL = whitelist locations</i>\n\n"
            "⏳ <b>Time remaining:</b> {days}\n"
            "Fingerprint: <code>{fingerprint}</code>"
        ),
        "unlimited": "Unlimited",
        "lifetime": "Lifetime",
    },
    "ru": {
        "info_text": (
            "ℹ️ <b>Ваш профиль:</b> <code>{username}</code>\n"
            "Включен: {status}\n"
            "Доступ к WL-локациям: {wl_status}\n"
            "В сети: {online}\n\n"
            "📊 <b>Трафик:</b>\n"
            "Скачано за всё время: {total}\n"
            "В этом месяце: {monthly}\n"
            "WL за всё время: {wl_total}\n"
            "WL в этом месяце: {wl_monthly}\n"
            "Подробная информация:\n"
            "↑ {up} / ↓ {down}\n"
            "WL: ↑ {wl_up} / ↓ {wl_down}\n\n"
            "<i>WL = whitelist-локации</i>\n\n"
            "⏳ <b>Осталось времени:</b> {days}\n"
            "Отпечаток: <code>{fingerprint}</code>"
        ),
        "unlimited": "Безлимит",
        "lifetime": "Навсегда",
    },
}


@pytest.fixture
def info_mixin() -> tuple[PublicSubscriptionMixin, MagicMock, MagicMock]:
    mixin = PublicSubscriptionMixin.__new__(PublicSubscriptionMixin)
    telegram = MagicMock()
    subscription = MagicMock()
    mixin.bot = cast(Any, telegram)
    mixin.sub = cast(Any, subscription)
    mixin.TEXTS = INFO_TEXTS
    mixin.get_menu = MagicMock(return_value="menu")  # type: ignore[method-assign]
    return mixin, telegram, subscription


def _info(**overrides: Any) -> UserInfo:
    bandwidth = UserInfoBandwidth(
        total=UserInfoBandwidthTotal(
            upload=13_785_700_000,
            download=113_972_680_000,
            total=127_758_370_000,
        ),
        wl_total=UserInfoBandwidthTotal(upload=0, download=0, total=0),
        monthly=0,
        wl_monthly=0,
        limit=0,
        wl_limit=5,
    )
    payload: dict[str, Any] = {
        "_": "test",
        "token": "t" * 40,
        "link": "https://example.test/sub?token=t",
        "displayname": "PomiDor",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "fingerprint": "edge",
        "enabled": True,
        "wl_enabled": True,
        "time": 0,
        "online": True,
        "bandwidth": bandwidth,
    }
    payload.update(overrides)
    return UserInfo(**payload)


def test_english_info_uses_auto_units_and_html(
    info_mixin: tuple[PublicSubscriptionMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = info_mixin
    subscription.telegram_svc.get_info_telegram.return_value = _info()
    mixin.send_info(7, 42, "en")
    telegram.send_message.assert_called_once()
    args, kwargs = telegram.send_message.call_args
    text = args[1]
    assert kwargs["parse_mode"] == "HTML"
    assert "Total downloaded: 127.76 GB" in text
    assert "This month: <i>Unlimited</i>" in text
    assert "WL traffic total: 0 B" in text
    assert "WL traffic this month: 0 B / 5 GB" in text
    assert "↑ 13.79 GB / ↓ 113.97 GB" in text
    assert "WL: ↑ 0 B / ↓ 0 B" in text
    assert "<i>WL = whitelist locations</i>" in text
    assert "⏳ <b>Time remaining:</b> Lifetime" in text
    assert "Fingerprint: <code>edge</code>" in text
    assert "Unlimited MB" not in text


def test_russian_info_matches_english_layout(
    info_mixin: tuple[PublicSubscriptionMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = info_mixin
    subscription.telegram_svc.get_info_telegram.return_value = _info()
    mixin.send_info(7, 42, "ru")
    text = telegram.send_message.call_args.args[1]
    assert "Скачано за всё время: 127.76 GB" in text
    assert "В этом месяце: <i>Безлимит</i>" in text
    assert "WL за всё время: 0 B" in text
    assert "WL в этом месяце: 0 B / 5 GB" in text
    assert "Отпечаток: <code>edge</code>" in text
    assert "⏳ <b>Осталось времени:</b> Навсегда" in text
