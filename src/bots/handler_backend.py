"""Thread-safe in-memory storage for pyTelegramBotAPI next-step handlers."""

from __future__ import annotations

import threading
from typing import Any

from telebot.handler_backends import HandlerBackend

__all__ = ["LockedHandlerBackend"]


class LockedHandlerBackend(HandlerBackend):
    """MemoryHandlerBackend with one lock around every handlers-dict mutation.

    The stock backend is an unlocked dict. Public-bot login and settings register
    a next-step handler from one worker while /start or the next message can
    clear or pop that same chat from another.
    """

    def __init__(self) -> None:
        super().__init__({})
        self._lock = threading.Lock()

    def register_handler(self, handler_group_id: int | str, handler: Any) -> None:
        with self._lock:
            bucket = self.handlers.get(handler_group_id)
            if bucket is None:
                self.handlers[handler_group_id] = [handler]
            else:
                bucket.append(handler)

    def clear_handlers(self, handler_group_id: int | str) -> None:
        with self._lock:
            self.handlers.pop(handler_group_id, None)

    def get_handlers(self, handler_group_id: int | str) -> list[Any] | None:
        with self._lock:
            return self.handlers.pop(handler_group_id, None)
