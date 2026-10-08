"""Application logging.

``Logger`` is the process logger used by the app and by the Discord service.
Its threshold is ``LOGLEVEL`` (default DEBUG) unless a caller passes a level.
``GunicornLogger`` is the journal access logger configured by gunicorn. It is
not imported here: importing this package must not pull gunicorn into a
process that only needs ``Logger``. Import it from ``loggers.access``.
HTTP access logs use ``LOGLEVEL_GUNICORN`` (default DEBUG). That variable does
not change which status class maps to TRACE, DEBUG, or INFO.
"""

from __future__ import annotations

from .colors import Colors
from .common import _ANSI_ESCAPE, _safe_handle_error
from .handlers import _JSONLinesLogger, _TelegramLogger
from .level import LEVELS, TRACE, env_level, parse_level
from .logger import Logger

__all__ = [
    "Logger",
    "LEVELS",
    "TRACE",
    "Colors",
    "env_level",
    "parse_level",
    "_ANSI_ESCAPE",
    "_JSONLinesLogger",
    "_TelegramLogger",
    "_safe_handle_error",
]
