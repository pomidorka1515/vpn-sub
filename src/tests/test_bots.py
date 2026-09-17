from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, cast

from bots import PublicBot, TelegramPollingMixin


_STOP_WAIT_SECONDS = 30


class _NullLogger:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)


class _Poller:
    def __init__(self) -> None:
        self.stop_polling_calls = 0
        self.polling_started = threading.Event()

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
