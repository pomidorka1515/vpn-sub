from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from .common import _ANSI_ESCAPE, _safe_handle_error

if TYPE_CHECKING:
    from bots import AdminBot
    from config import LinesConfigLike

__all__ = ["_JSONLinesLogger", "_TelegramLogger"]


class _TelegramLogger(logging.Handler):
    def __init__(self, bot: AdminBot):
        """
        Logging handler to broadcast messages to a telegram bot (AdminBot).
        """
        super().__init__()
        self.bot = bot
        self.ansi_escape = _ANSI_ESCAPE

    def emit(self, record: logging.LogRecord, **kwargs: str) -> None:
        """
        Args:
            record: The record to broadcast.
            **kwargs: Extra keyword arguments to pass into AdminBot.msg.
        """
        try:
            msg_text = self.format(record)
            clean_text = self.ansi_escape.sub('', msg_text)
            safe_text = html.escape(clean_text)
            self.bot.msg(f"<code>{safe_text}</code>", **kwargs)
        except Exception:
            _safe_handle_error(self, record)


class _JSONLinesLogger(logging.Handler):
    def __init__(self, config: LinesConfigLike):
        """
        Logging handler to write logs into a .jsonl file.
        Uses LinesConfig manager.
        """
        super().__init__()
        self.cfg = config
        self.ansi_escape = _ANSI_ESCAPE

    def emit(self, record: logging.LogRecord) -> None:
        """
        Args:
            record: The record to broadcast.
        """
        try:
            timestamp = record.created
            date = datetime.fromtimestamp(timestamp).strftime("%d.%m.%Y %H:%M:%S")
            level = record.levelname
            log_name = record.name
            thread_name = record.threadName
            text = record.getMessage()

            to_log: dict[str, str | float | None] = {
                "ts": timestamp,
                "date": date,
                "level": level,
                "name": log_name,
                "threadname": thread_name,
                "text": text
            }

            self.cfg.append(record=to_log)
        except Exception:
            _safe_handle_error(self, record)
