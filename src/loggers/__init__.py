"""Application logging.

``Logger`` is the process logger used by the app and by the Discord service.
``GunicornLogger`` is the journal access logger configured by gunicorn. It is
not imported here: importing this package must not pull gunicorn into a
process that only needs ``Logger``. Import it from ``loggers.access``.
"""

from __future__ import annotations

from .colors import Colors
from .common import _ANSI_ESCAPE, _safe_handle_error
from .handlers import _JSONLinesLogger, _TelegramLogger
from .logger import TRACE, Logger

__all__ = [
    "Logger",
    "TRACE",
    "Colors",
    "_ANSI_ESCAPE",
    "_JSONLinesLogger",
    "_TelegramLogger",
    "_safe_handle_error",
]
