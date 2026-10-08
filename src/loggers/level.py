from __future__ import annotations

import logging
import math
import os

__all__ = ["LEVELS", "TRACE", "env_level", "parse_level"]

TRACE = 5
logging.addLevelName(TRACE, "TRACE")

# Named levels, including TRACE. Aliases match what operators type.
LEVELS: dict[str, int] = {
    "TRACE": TRACE,
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
    "FATAL": logging.CRITICAL,
}

# Thresholds in descending order so an int floors to the nearest named level.
_THRESHOLDS: tuple[int, ...] = (
    logging.CRITICAL,
    logging.ERROR,
    logging.WARNING,
    logging.INFO,
    logging.DEBUG,
    TRACE,
)


def parse_level(raw: str) -> int:
    """Parse a case-insensitive level name, or an int floored to a named level.

    ``"info"`` and ``"INFO"`` are INFO. ``"15"`` is DEBUG (10), not a custom
    level between DEBUG and INFO. Values at or above CRITICAL stay CRITICAL.
    Values below TRACE stay TRACE. Blank and unknown names raise ``ValueError``.
    """
    text = raw.strip()
    if not text:
        raise ValueError("log level is empty")
    named = LEVELS.get(text.upper())
    if named is not None:
        return named
    try:
        number = float(text)
    except ValueError:
        raise ValueError(f"unknown log level: {raw!r}") from None
    if not math.isfinite(number):
        raise ValueError(f"unknown log level: {raw!r}")
    floored = math.floor(number)
    for threshold in _THRESHOLDS:
        if floored >= threshold:
            return threshold
    return TRACE


def env_level(name: str, default: int) -> int:
    """Read ``name`` from the environment. Unset uses ``default``.

    An empty or unrecognized value also uses ``default``: a bad operator
    setting must not stop the process from logging.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return parse_level(raw)
    except ValueError:
        return default
