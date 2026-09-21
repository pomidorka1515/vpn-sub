from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Callable, cast
from unittest.mock import MagicMock, patch

from telebot import types

from bots import AdminBot, PublicBot
from bots.polling import TelegramPollingMixin


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
    def __init__(self) -> None:
        self.stop_polling_calls = 0
        self.polling_started = threading.Event()
        self.token: str | None = None

    def infinity_polling(self, **kwargs: Any) -> None:
        self.polling_started.set()
        stop_event.wait(_STOP_WAIT_SECONDS)

    def stop_polling(self) -> None:
        self.stop_polling_calls += 1


class _TestBot(TelegramPollingMixin):
    def __init__(self) -> None:
        self.poller = _Poller()
        self.logger = _NullLogger()
        self.bot = cast(Any, self.poller)
        self.log = cast(Any, self.logger)


stop_event = threading.Event()


class PollingLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        stop_event.clear()

    def test_start_polling_reuses_running_thread(self) -> None:
        bot = _TestBot()
        bot.start_polling()
        assert bot.polling_thread is not None
        thread = bot.polling_thread
        self.assertTrue(bot.poller.polling_started.wait(1))

        bot.start_polling()
        self.assertIs(bot.polling_thread, thread)

        stop_event.set()
        bot.stop_polling()
        self.assertFalse(thread.is_alive())
        self.assertEqual(bot.logger.errors, [])

    def test_stop_polling_is_idempotent_and_does_not_join_without_thread(self) -> None:
        bot = _TestBot()
        started = time.monotonic()
        bot.stop_polling()
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertEqual(bot.poller.stop_polling_calls, 1)

        bot.start_polling()
        self.assertTrue(bot.poller.polling_started.wait(1))
        stop_event.set()
        bot.stop_polling()
        bot.stop_polling()
        self.assertGreaterEqual(bot.poller.stop_polling_calls, 3)

    def test_stop_polling_logs_timeout(self) -> None:
        bot = _TestBot()
        bot.start_polling()
        assert bot.polling_thread is not None
        thread = bot.polling_thread
        self.assertTrue(bot.poller.polling_started.wait(1))

        bot.stop_polling()
        self.assertTrue(thread.is_alive())
        self.assertEqual(len(bot.logger.errors), 1)

        stop_event.set()
        thread.join(_STOP_WAIT_SECONDS)
        self.assertFalse(thread.is_alive())

    def test_polling_errors_are_logged_without_token_or_traceback(self) -> None:
        bot = _TestBot()
        token = "123456:secret-token-value"
        bot.poller.token = token
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

        bot.poller.infinity_polling = infinity_polling  # type: ignore[method-assign]
        bot.start_polling()
        self.assertTrue(raised.wait(1))
        deadline = time.monotonic() + 1
        while not bot.logger.warnings and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertEqual(len(bot.logger.warnings), 1)
        warning = bot.logger.warnings[0]
        self.assertIn("telegram polling failed:", warning)
        self.assertIn("ConnectionError", warning)
        self.assertNotIn(token, warning)
        self.assertNotIn("secret-token-value", warning)
        self.assertNotIn("Traceback", warning)
        self.assertEqual(bot.logger.errors, [])

        stop_event.set()
        bot.stop_polling()


class PublicTextRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = PublicBot.__new__(PublicBot)
        self.telegram = MagicMock()
        self.subscription = MagicMock()
        self.bot.bot = cast(Any, self.telegram)
        self.bot.sub = cast(Any, self.subscription)
        self.subscription.get_telegram_language.return_value = "en"
        self.bot.TEXTS = {
            lang: {button: f"{lang}:{button}" for button, _, _ in self.bot.ROUTES}
            for lang in ("ru", "en")
        }

    def message(self, text: str) -> types.Message:
        return cast(
            types.Message,
            SimpleNamespace(text=text, from_user=SimpleNamespace(id=42), chat=SimpleNamespace(id=7)),
        )

    def test_routes_both_languages_to_first_matching_handler(self) -> None:
        for button, handler_name, _requires_reg in self.bot.ROUTES:
            for lang in ("ru", "en"):
                with self.subTest(button=button, lang=lang):
                    message = self.message(self.bot.TEXTS[lang][button])
                    with patch.object(PublicBot, handler_name) as handler:
                        self.bot.handle_text(message)
                    handler.assert_called_once_with(message, 42, "en")

    def test_unknown_text_is_ignored(self) -> None:
        self.bot.handle_text(self.message("not a button"))
        self.telegram.send_message.assert_not_called()
        self.subscription.is_registered.assert_not_called()

    def test_chart_buttons_keep_original_order_and_registration_guard(self) -> None:
        self.bot.TEXTS["en"].update(
            btn_chart_days="Last {days} days", choose_chart_days="Choose a period"
        )
        message = self.message(self.bot.TEXTS["ru"]["btn_chart"])
        self.subscription.is_registered.return_value = False
        self.bot.handle_text(message)
        self.telegram.send_message.assert_not_called()

        self.subscription.is_registered.return_value = True
        self.bot.handle_text(message)
        args, kwargs = self.telegram.send_message.call_args
        self.assertEqual(args, (7, "Choose a period"))
        markup = kwargs["reply_markup"]
        self.assertEqual(
            [button.callback_data for row in markup.keyboard for button in row],
            ["chart_3", "chart_14", "chart_30", "chart_90"],
        )

    def test_registered_routes_are_blocked_before_handler_dispatch(self) -> None:
        self.subscription.is_registered.return_value = False
        message = self.message(self.bot.TEXTS["en"]["btn_info"])

        with patch.object(PublicBot, "_handle_info") as handler:
            self.bot.handle_text(message)

        handler.assert_not_called()
        self.subscription.is_registered.assert_called_once_with(42)


class AdminCallbackRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = AdminBot.__new__(AdminBot)
        self.telegram = MagicMock()
        self.bot.bot = cast(Any, self.telegram)
        self.bot.admin_uids = [42]
        self.bot.log = MagicMock()

    def callback(self, data: str, user_id: int = 42) -> types.CallbackQuery:
        return cast(
            types.CallbackQuery,
            SimpleNamespace(
                id="callback-id",
                data=data,
                from_user=SimpleNamespace(id=user_id),
                message=SimpleNamespace(
                    message_id=11,
                    chat=SimpleNamespace(id=7),
                ),
            ),
        )

    def test_routes_exact_and_prefix_callbacks_to_first_matching_handler(self) -> None:
        for route, handler_name, is_prefix in self.bot.ROUTES:
            with self.subTest(route=route):
                data = f"{route}value" if is_prefix else route
                call = self.callback(data)
                with patch.object(AdminBot, handler_name) as handler:
                    self.bot.handle_callbacks(call)
                handler.assert_called_once_with(data, 7, call.message)

    def test_overlapping_routes_keep_specific_callbacks_first(self) -> None:
        call = self.callback("info_code")
        with (
            patch.object(AdminBot, "_handle_info_code") as info_code,
            patch.object(AdminBot, "_handle_info_user") as info_user,
        ):
            self.bot.handle_callbacks(call)
        info_code.assert_called_once_with("info_code", 7, call.message)
        info_user.assert_not_called()

        call = self.callback("edit_user_alice")
        with (
            patch.object(AdminBot, "_handle_edit_user") as edit_user,
            patch.object(AdminBot, "_handle_edit") as edit_action,
        ):
            self.bot.handle_callbacks(call)
        edit_user.assert_called_once_with("edit_user_alice", 7, call.message)
        edit_action.assert_not_called()

    def test_unknown_callback_is_acknowledged_and_ignored(self) -> None:
        self.bot.handle_callbacks(self.callback("unknown"))

        self.telegram.answer_callback_query.assert_called_once_with("callback-id")
        self.telegram.send_message.assert_not_called()
        cast(MagicMock, self.bot.log).error.assert_not_called()

    def test_non_admin_callback_is_ignored_without_acknowledgement(self) -> None:
        self.bot.handle_callbacks(self.callback("list_users", user_id=99))

        self.telegram.answer_callback_query.assert_not_called()


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


class ExecutorShutdownTests(unittest.TestCase):
    def test_public_bot_stop_does_not_wait_for_running_chart(self) -> None:
        bot = _ChartOnlyPublicBot()
        release = threading.Event()
        future = bot.submit(release.wait)

        bot.stop()
        self.assertTrue(bot.stopped_polling)
        self.assertFalse(future.done())

        release.set()
        bot.close()


if __name__ == "__main__":
    unittest.main()
