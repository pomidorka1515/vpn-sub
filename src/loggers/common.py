from __future__ import annotations

import re
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import logging

__all__ = ["_ANSI_ESCAPE", "_safe_handle_error"]

_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _safe_handle_error(handler: logging.Handler, record: logging.LogRecord) -> None:
    try:
        handler.handleError(record)
    except Exception:
        print(f"logging handler {handler!r} failed while handling {record!r}", file=sys.stderr)  # noqa: T201
