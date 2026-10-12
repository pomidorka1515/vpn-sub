"""Small mechanics shared by Telegram bot feature mixins."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, TypedDict, Unpack, cast

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from telebot import types
    from telebot.async_telebot import AsyncTeleBot

    from config import AppConfig, LangConfig
    from core import AsyncSubscription
    from core.async_common import Snapshot
    from loggers import Logger

__all__ = ["AdminStateMixin", "BotStateMixin", "PublicStateMixin", "TelegramIOMixin"]


class MessageOptions(TypedDict, total=False):
    parse_mode: str
    reply_markup: types.InlineKeyboardMarkup | types.ReplyKeyboardMarkup


class TelegramIOMixin:
    bot: AsyncTeleBot
    log: Logger

    async def _answer_callback(self, call_id: str, text: str | None = None) -> None:
        # pyTelegramBotAPI annotates callback_query_id as int, but Telegram
        # callback ids are strings (CallbackQuery.id: str).
        if text is None:
            await self.bot.answer_callback_query(cast(Any, call_id))
        else:
            await self.bot.answer_callback_query(cast(Any, call_id), text)

    async def _send_message(
        self, chat_id: int, text: str, **kwargs: Unpack[MessageOptions]
    ) -> None:
        try:
            await self.bot.send_message(chat_id, text, **kwargs)
        except Exception:
            self.log.exception("failed to send message to chat %s", chat_id)

    async def _delete_message(self, chat_id: int, message_id: int, *, secret: bool = False) -> None:
        try:
            await self.bot.delete_message(chat_id, message_id)
        except Exception as error:
            level = logging.CRITICAL if secret else logging.ERROR
            prefix = "secret" if secret else "message"
            self.log.log(
                level,
                f"failed to delete {prefix} {message_id} in chat {chat_id}: {error}",
                exc_info=True,
            )
            if secret:
                await self._send_message(chat_id, "⚠️ Не удалось удалить ваше сообщение с секретом.")


class BotStateMixin:
    """Typing contract for state initialized by concrete bot composition roots."""

    cfg: Snapshot[AppConfig]
    lang_cfg: Snapshot[LangConfig]
    sub: AsyncSubscription


class AdminStateMixin(BotStateMixin):
    admin_uids: list[int]
    _pending_codes: MutableMapping[str | int, str]
    _pending_edits: MutableMapping[str | int, dict[str, str]]
    _pagination_state: MutableMapping[str | int, dict[str, int]]
    _pending_leaderboard: MutableMapping[str | int, dict[str, str | int]]


class PublicStateMixin(BotStateMixin):
    TEXTS: dict[str, dict[str, str]]
