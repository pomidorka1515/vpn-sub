from __future__ import annotations

import asyncio
import html
import logging
import queue
import sys
import threading
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .common import _ANSI_ESCAPE

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from config import JsonDict
    from config.async_store import AsyncLinesConfig

_IN_LOG_DELIVERY: ContextVar[bool] = ContextVar("in_log_delivery", default=False)


def stderr_fallback(message: str) -> None:
    """Last-resort important-record/error visibility without recursive logging."""
    sys.stderr.write(message + "\n")


class LogDelivery[T]:
    """Bounded thread-to-loop delivery with one owned, serial consumer.

    Drop newest on overflow; ERROR+ falls back synchronously to stderr.
    Delivery errors also go directly to stderr, never through alert logging.
    A failed shutdown deadline raises and leaves the consumer owned/alive;
    callers must keep its dependencies open and may await close again.
    """

    def __init__(
        self,
        deliver: Callable[[T], Awaitable[None]],
        *,
        name: str,
        capacity: int = 256,
        drain_timeout: float = 5,
        fallback: Callable[[str], None] = stderr_fallback,
    ) -> None:
        if capacity < 1 or drain_timeout <= 0:
            raise ValueError("capacity and drain_timeout must be positive")
        self._deliver = deliver
        self._name = name
        self._queue: queue.Queue[T] = queue.Queue(maxsize=capacity)
        self._drain_timeout = drain_timeout
        self._fallback = fallback
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._accepting = False
        self._closed = False
        self.dropped = 0
        self.failed = 0

    async def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("log delivery is closed")
            if self._task is None:
                self._loop = asyncio.get_running_loop()
                self._accepting = True
                self._task = asyncio.create_task(self._consume(), name=self._name)

    def submit(self, value: T, *, important: bool, fallback_text: str) -> None:
        accepted = False
        with self._lock:
            if self._accepting and self._loop is not None:
                try:
                    self._queue.put_nowait(value)
                    # Only an empty-to-nonempty transition needs a loop wakeup.
                    if self._queue.qsize() == 1:
                        self._loop.call_soon_threadsafe(self._wake.set)
                    accepted = True
                except queue.Full:
                    pass
            if not accepted:
                self.dropped += 1
        if not accepted and important:
            self._fallback(fallback_text)

    async def _consume(self) -> None:
        while True:
            self._wake.clear()
            while True:
                try:
                    value = self._queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    token = _IN_LOG_DELIVERY.set(True)
                    try:
                        await self._deliver(value)
                    finally:
                        _IN_LOG_DELIVERY.reset(token)
                except Exception:
                    self.failed += 1
                    # Do not print exception strings: transport errors can
                    # contain bot tokens or credential-bearing URLs.
                    self._fallback(f"{self._name}: log delivery failed")
                finally:
                    self._queue.task_done()
            with self._lock:
                if self._closed and self._queue.empty():
                    return
            await self._wake.wait()

    async def close(self) -> None:
        with self._lock:
            self._accepting = False
            self._closed = True
            self._wake.set()
        if self._task is not None:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=self._drain_timeout)


class AsyncJSONLinesLogger(logging.Handler):
    def __init__(self, delivery: LogDelivery[JsonDict]) -> None:
        super().__init__()
        self.delivery = delivery

    def emit(self, record: logging.LogRecord) -> None:
        if _IN_LOG_DELIVERY.get():
            return
        try:
            text = _ANSI_ESCAPE.sub("", self.format(record))
            payload: JsonDict = {
                "ts": record.created,
                "date": datetime.fromtimestamp(record.created, tz=UTC).strftime(
                    "%d.%m.%Y %H:%M:%S"
                ),
                "level": record.levelname,
                "name": record.name,
                "threadname": record.threadName,
                "taskname": record.taskName,
                "text": text,
            }
            self.delivery.submit(
                payload, important=record.levelno >= logging.ERROR, fallback_text=text
            )
        except Exception:
            self.handleError(record)


class AsyncTelegramLogger(logging.Handler):
    def __init__(self, delivery: LogDelivery[str]) -> None:
        super().__init__()
        self.delivery = delivery

    def emit(self, record: logging.LogRecord) -> None:
        if _IN_LOG_DELIVERY.get():
            return
        try:
            text = _ANSI_ESCAPE.sub("", self.format(record))
            self.delivery.submit(
                f"<code>{html.escape(text)}</code>",
                important=record.levelno >= logging.ERROR,
                fallback_text=text,
            )
        except Exception:
            self.handleError(record)


def json_lines_delivery(store: AsyncLinesConfig, *, capacity: int = 256) -> LogDelivery[JsonDict]:
    return LogDelivery(store.append, name="logs:jsonl", capacity=capacity)
