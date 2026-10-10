"""Thread-safe in-memory storage for pyTelegramBotAPI next-step handlers."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from telebot.handler_backends import HandlerBackend

if TYPE_CHECKING:
    from telebot import Handler

__all__ = ["LockedHandlerBackend"]


class LockedHandlerBackend(HandlerBackend):
    """MemoryHandlerBackend with one lock around every handlers-dict mutation.

    The stock backend is an unlocked dict. Public-bot login and settings register
    a next-step handler from one worker while /start or the next message can
    clear or pop that same chat from another.
    """

    handlers: dict[int | str, list[Handler]]

    def __init__(self) -> None:
        # The upstream initializer only assigns this dictionary and is untyped.
        super().__init__({})  # type: ignore[no-untyped-call] # pyright: ignore[reportUnknownMemberType]
        self._lock = threading.Lock()

    def register_handler(self, handler_group_id: int | str, handler: Handler) -> None:
        with self._lock:
            bucket = self.handlers.get(handler_group_id)
            if bucket is None:
                self.handlers[handler_group_id] = [handler]
            else:
                bucket.append(handler)

    def clear_handlers(self, handler_group_id: int | str) -> None:
        with self._lock:
            self.handlers.pop(handler_group_id, None)

    def get_handlers(self, handler_group_id: int | str) -> list[Handler] | None:
        with self._lock:
            return self.handlers.pop(handler_group_id, None)
