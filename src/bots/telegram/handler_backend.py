"""Expiring, user-scoped conversations for AsyncTeleBot handlers."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from telebot import types

type ConversationKey = tuple[int, int]
type Step = Callable[..., Awaitable[None]]


@dataclass(slots=True)
class Conversation:
    step: Step
    arguments: tuple[object, ...]
    expires: float


class ConversationRouter:
    """One instance per bot; locks exist only while handlers borrow them."""

    def __init__(self, *, ttl: float = 600, capacity: int = 1024) -> None:
        self._states: dict[ConversationKey, Conversation] = {}
        self._locks: dict[ConversationKey, tuple[asyncio.Lock, int]] = {}
        self.ttl = ttl
        self.capacity = capacity

    def prune(self) -> None:
        now = time.monotonic()
        for key, state in tuple(self._states.items()):
            if state.expires <= now:
                del self._states[key]

    def expect(self, key: ConversationKey, step: Step, *arguments: object) -> None:
        self.prune()
        if key not in self._states and len(self._states) >= self.capacity:
            raise RuntimeError("conversation capacity exceeded")
        self._states[key] = Conversation(step, arguments, time.monotonic() + self.ttl)

    def clear(self, key: ConversationKey) -> None:
        self._states.pop(key, None)

    def clear_all(self) -> None:
        self._states.clear()

    async def dispatch(self, key: ConversationKey, message: types.Message) -> bool:
        self.prune()
        state = self._states.pop(key, None)
        if state is None:
            return False
        await state.step(message, *state.arguments)
        return True

    @asynccontextmanager
    async def serial(self, key: ConversationKey) -> AsyncGenerator[None]:
        lock, users = self._locks.get(key, (asyncio.Lock(), 0))
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            _, users = self._locks[key]
            if users == 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)
