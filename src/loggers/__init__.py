"""Application logging.

``Logger`` is the process logger used by the app and by the Discord service.
Its threshold is ``LOGLEVEL`` (default DEBUG) unless a caller passes a level.
HTTP access logs use ``LOGLEVEL_HTTP`` (default INFO). That variable does
not change which status class maps to TRACE, DEBUG, or INFO.
"""

from __future__ import annotations

from .colors import Colors, color_status
from .common import _ANSI_ESCAPE, _safe_handle_error
from .level import LEVELS, TRACE, env_level, parse_level
from .logger import Logger

__all__ = [
    "LEVELS",
    "TRACE",
    "_ANSI_ESCAPE",
    "Colors",
    "Logger",
    "_safe_handle_error",
    "color_status",
    "env_level",
    "parse_level",
]
