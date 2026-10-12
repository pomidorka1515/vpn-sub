"""User-scoped, expiring callback wizard values."""

from __future__ import annotations

import time
from collections.abc import MutableMapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextvars import ContextVar

    from .handler_backend import ConversationKey


class PendingValues[T](MutableMapping[int | str, T]):
    """Expose chat keys to features, partition storage by the active user."""

    def __init__(self, conversation: ContextVar[ConversationKey], *, ttl: float = 600) -> None:
        self._conversation = conversation
        self._values: dict[tuple[int | str, int], tuple[float, T]] = {}
        self.ttl = ttl

    def _key(self, chat: int | str) -> tuple[int | str, int]:
        return chat, self._conversation.get()[1]

    def _prune(self) -> None:
        now = time.monotonic()
        for key, (expires, _) in tuple(self._values.items()):
            if expires <= now:
                del self._values[key]

    def __getitem__(self, chat: int | str) -> T:
        self._prune()
        return self._values[self._key(chat)][1]

    def __setitem__(self, chat: int | str, value: T) -> None:
        self._prune()
        if len(self._values) >= 1024 and self._key(chat) not in self._values:
            raise RuntimeError("callback conversation capacity exceeded")
        self._values[self._key(chat)] = time.monotonic() + self.ttl, value

    def __delitem__(self, chat: int | str) -> None:
        del self._values[self._key(chat)]

    def __iter__(self) -> Iterator[int | str]:
        self._prune()
        uid = self._conversation.get()[1]
        return iter(chat for chat, user in self._values if user == uid)

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def clear(self) -> None:
        self._values.clear()
