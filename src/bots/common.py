"""Small mechanics shared by Telegram bot feature mixins."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import telebot

from loggers import Logger
from config import ConfigLike
from core import Subscription
__all__ = ["AdminStateMixin", "BotStateMixin", "PublicStateMixin", "TelegramIOMixin"]



class TelegramIOMixin:
    bot: telebot.TeleBot
    log: Logger

    def _send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
        try:
            self.bot.send_message(chat_id, text, **cast(Any, kwargs))
        except Exception as error:
            self.log.error(f"failed to send message to chat {chat_id}: {error}", exc_info=True)

    def _delete_message(self, chat_id: int, message_id: int, *, secret: bool = False) -> None:
        try:
            self.bot.delete_message(chat_id, message_id)
        except Exception as error:
            level = logging.CRITICAL if secret else logging.ERROR
            prefix = "secret" if secret else "message"
            self.log.log(
                level,
                f"failed to delete {prefix} {message_id} in chat {chat_id}: {error}",
                exc_info=True,
            )
            if secret:
                self._send_message(chat_id, "⚠️ Не удалось удалить ваше сообщение с секретом.")


class BotStateMixin:
    """Typing contract for state initialized by concrete bot composition roots."""

    cfg: ConfigLike
    lang_cfg: ConfigLike
    sub: Subscription
    polling_thread: threading.Thread | None


class AdminStateMixin(BotStateMixin):
    admin_uids: list[int]
    _pending_codes: dict[str | int, str]
    _pending_edits: dict[int, dict[str, str]]
    _pagination_state: dict[int, dict[str, int]]
    _pending_leaderboard: dict[int, dict[str, str | int]]


class PublicStateMixin(BotStateMixin):
    TEXTS: dict[str, dict[str, str]]
    _executor: ThreadPoolExecutor


