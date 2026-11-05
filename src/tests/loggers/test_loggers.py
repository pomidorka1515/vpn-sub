from __future__ import annotations

import logging

import pytest

from loggers import TRACE, Colors, Logger
from loggers.level import env_level, parse_level


def test_colors_are_ansi_strings() -> None:
    message = f"{Colors.RED}Hello {Colors.GREEN}World!{Colors.RESET} regular"
    assert message == "\033[31mHello \033[32mWorld!\033[0m regular"
    assert Colors.BOLD == "\033[1m"
    assert Colors.DIM == "\033[2m"
    assert Colors.ITALIC == "\033[3m"
    assert Colors.UNDERLINE == "\033[4m"
    assert Colors.REVERSE == "\033[7m"
    assert Colors.STRIKE == "\033[9m"
    assert Logger.RESET is Colors.RESET
    assert Logger.COLORS["ERROR"] is Colors.RED
    assert Logger.COLORS["TRACE"] is Colors.GREY
    assert logging.getLevelName(TRACE) == "TRACE"


def test_logger_trace_is_below_debug() -> None:
    stored: list[tuple[int, str]] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            stored.append((record.levelno, record.getMessage()))

    logger = Logger("trace")
    logger.handlers.clear()
    logger.addHandler(_Handler())
    logger.propagate = False
    logger.setLevel(TRACE)

    logger.trace("poll %s", "ok")
    assert stored == [(TRACE, "poll ok")]

    logger.setLevel(logging.DEBUG)
    assert not logger.isEnabledFor(TRACE)
    assert logger.isEnabledFor(logging.DEBUG)


def test_parse_level_accepts_names_and_floors_ints() -> None:
    assert parse_level("trace") == TRACE
    assert parse_level("DEBUG") == logging.DEBUG
    assert parse_level("Info") == logging.INFO
    assert parse_level("warn") == logging.WARNING
    assert parse_level("warning") == logging.WARNING
    assert parse_level("error") == logging.ERROR
    assert parse_level("critical") == logging.CRITICAL
    assert parse_level("fatal") == logging.CRITICAL
    assert parse_level("  info  ") == logging.INFO

    assert parse_level("0") == TRACE
    assert parse_level("5") == TRACE
    assert parse_level("9") == TRACE
    assert parse_level("10") == logging.DEBUG
    assert parse_level("15") == logging.DEBUG
    assert parse_level("19.9") == logging.DEBUG
    assert parse_level("20") == logging.INFO
    assert parse_level("29") == logging.INFO
    assert parse_level("30") == logging.WARNING
    assert parse_level("40") == logging.ERROR
    assert parse_level("50") == logging.CRITICAL
    assert parse_level("99") == logging.CRITICAL
    assert parse_level("-3") == TRACE

    with pytest.raises(ValueError, match="log level is empty"):
        parse_level("")
    with pytest.raises(ValueError, match="unknown log level"):
        parse_level("verbose")
    with pytest.raises(ValueError, match="unknown log level"):
        parse_level("nan")


def test_env_level_reads_loglevel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOGLEVEL", raising=False)
    assert env_level("LOGLEVEL", logging.DEBUG) == logging.DEBUG

    monkeypatch.setenv("LOGLEVEL", "warning")
    assert env_level("LOGLEVEL", logging.DEBUG) == logging.WARNING

    monkeypatch.setenv("LOGLEVEL", "15")
    assert env_level("LOGLEVEL", logging.DEBUG) == logging.DEBUG

    monkeypatch.setenv("LOGLEVEL", "nope")
    assert env_level("LOGLEVEL", logging.INFO) == logging.INFO

    monkeypatch.setenv("LOGLEVEL", "   ")
    assert env_level("LOGLEVEL", logging.DEBUG) == logging.DEBUG


def test_logger_uses_loglevel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOGLEVEL", "warning")
    logger = Logger("env-level")
    assert logger.level == logging.WARNING
    assert not logger.isEnabledFor(logging.INFO)

    overridden = Logger("env-level-override", logging.DEBUG)
    assert overridden.level == logging.DEBUG

    monkeypatch.setenv("LOGLEVEL", "12")
    floored = Logger("env-level-int")
    assert floored.level == logging.DEBUG
