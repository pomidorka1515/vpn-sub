from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .common import _ANSI_ESCAPE, _safe_handle_error

if TYPE_CHECKING:
    from config import LinesConfig

__all__ = ["_JSONLinesLogger"]


class _JSONLinesLogger(logging.Handler):
    def __init__(self, config: LinesConfig) -> None:
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
            date = datetime.fromtimestamp(timestamp, tz=UTC).strftime("%d.%m.%Y %H:%M:%S")
            level = record.levelname
            log_name = record.name
            thread_name = record.threadName
            text = self.ansi_escape.sub("", record.getMessage())

            to_log: dict[str, str | float | None] = {
                "ts": timestamp,
                "date": date,
                "level": level,
                "name": log_name,
                "threadname": thread_name,
                "text": text,
            }

            self.cfg.append(record=to_log)
        except Exception:
            _safe_handle_error(self, record)
