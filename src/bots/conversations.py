from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import TYPE_CHECKING

from .handler_backend import ConversationRouter

if TYPE_CHECKING:
    from telebot import types

    from app_runtime.tasks import TaskRegistry

    from .handler_backend import ConversationKey, Step


class ConversationMixin:
    _handlers: TaskRegistry

    def _initialize_conversations(self) -> None:
        self._conversations = ConversationRouter()
        self._conversation: ContextVar[ConversationKey] = ContextVar("conversation")

    def _expect(self, message: types.Message, step: Step, *arguments: object) -> None:
        del message  # Prompt messages originate from the bot, not the user.
        self._conversations.expect(self._conversation.get(), step, *arguments)

    def _clear_steps(self, chat_id: int) -> None:
        key = self._conversation.get()
        if key[0] == chat_id:
            self._conversations.clear(key)

    def _authorized(self, uid: int) -> bool:
        del uid
        return True

    def _menu_message(self, text: str) -> bool:
        del text
        return False

    async def _message_entry(self, message: types.Message) -> None:
        if message.from_user is None or message.text is None:
            return
        key = (message.chat.id, message.from_user.id)
        task = self._handlers.create(
            lambda: self._route_message(key, message), name=f"{type(self).__name__}:message"
        )
        await asyncio.shield(task)

    async def _route_message(self, key: ConversationKey, message: types.Message) -> None:
        async with self._conversations.serial(key):
            token = self._conversation.set(key)
            try:
                if not self._authorized(key[1]):
                    self._conversations.clear(key)
                    return
                text = message.text or ""
                if text.startswith("/") or self._menu_message(text):
                    self._conversations.clear(key)
                    self._clear_pending(key[0])
                    if text.startswith("/"):
                        await self.cmd_start(message)
                        return
                if await self._conversations.dispatch(key, message):
                    return
                await self.handle_text(message)
            finally:
                self._conversation.reset(token)

    async def _callback_entry(self, call: types.CallbackQuery) -> None:
        if call.message is None or call.data is None:
            return
        key = (call.message.chat.id, call.from_user.id)
        task = self._handlers.create(
            lambda: self._route_callback(key, call), name=f"{type(self).__name__}:callback"
        )
        await asyncio.shield(task)

    async def _route_callback(self, key: ConversationKey, call: types.CallbackQuery) -> None:
        async with self._conversations.serial(key):
            token = self._conversation.set(key)
            try:
                if not self._authorized(key[1]):
                    self._conversations.clear(key)
                    return
                # A callback chooses a new workflow; discard any text step.
                self._conversations.clear(key)
                await self.handle_callbacks(call)
            finally:
                self._conversation.reset(token)

    def _clear_pending(self, chat_id: int) -> None:
        del chat_id

    async def cmd_start(self, message: types.Message) -> None:
        raise NotImplementedError

    async def handle_text(self, message: types.Message) -> None:
        del message

    async def handle_callbacks(self, call: types.CallbackQuery) -> None:
        raise NotImplementedError
