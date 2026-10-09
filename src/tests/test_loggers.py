from __future__ import annotations

import logging
from datetime import timedelta
from types import SimpleNamespace
from collections.abc import Mapping
from typing import Any, cast

import pytest

from gunicorn.http.message import Request
from gunicorn.http.wsgi import Response

from loggers.access import GunicornLogger, _color_status
from loggers import Colors
from loggers import TRACE, Logger
from loggers.level import env_level, parse_level
from loggers.handlers import _JSONLinesLogger


class _Cfg:
    accesslog = "-"
    logconfig = None
    logconfig_dict = None
    logconfig_json = None
    syslog = False
    disable_redirect_access_to_syslog = False


def _environ(path: str) -> dict[str, str]:
    return {
        "REQUEST_METHOD": "GET",
        "RAW_URI": path,
        "PATH_INFO": path,
        "SERVER_PROTOCOL": "HTTP/1.1",
        "REMOTE_ADDR": "203.0.113.10",
        "HTTP_USER_AGENT": "dashboard",
    }


class _AccessLog:
    def __init__(self, logged: list[tuple[int, str]]) -> None:
        self._logged = logged

    def log(self, level: object, message: object, *_args: object, **_kwargs: object) -> None:
        if isinstance(level, bool) or not isinstance(level, int):
            raise AssertionError(f"unexpected log level {level!r}")
        self._logged.append((level, str(message)))


def test_access_log_levels_by_status_and_path(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[tuple[int, str]] = []
    logger = GunicornLogger.__new__(GunicornLogger)
    logger.cfg = cast(Any, _Cfg())
    logger.access_log = cast(Any, _AccessLog(logged))
    atoms: dict[str, object] = {"m": "GET", "H": "HTTP/1.1", "s": "200", "B": 12}

    def atoms_for(*args: object) -> dict[str, object]:
        del args
        return atoms

    monkeypatch.setattr(logger, "atoms", atoms_for)
    req = cast(Request, SimpleNamespace())

    logger.access(
        cast(Response, SimpleNamespace(status="200 OK", sent=12)),
        req,
        _environ("/sub/api/api/state/polling"),
        timedelta(milliseconds=4),
    )
    assert len(logged) == 1
    assert logged[0][0] == 5
    assert "/api/state/polling" in logged[0][1]

    atoms.update(s="401", B=18)
    logger.access(
        cast(Response, SimpleNamespace(status="401 UNAUTHORIZED", sent=18)),
        req,
        _environ("/sub/api/api/state/polling"),
        timedelta(milliseconds=4),
    )
    assert len(logged) == 2
    assert logged[1][0] == logging.INFO
    assert "401" in logged[1][1]
    assert "/api/state/polling" in logged[1][1]

    logged.clear()
    atoms.update(s="200", B=8)
    logger.access(
        cast(Response, SimpleNamespace(status="200 OK", sent=8)),
        req,
        _environ("/sub/api/api/health"),
        timedelta(milliseconds=4),
    )
    assert len(logged) == 1
    assert logged[0][0] == logging.DEBUG
    assert "200" in logged[0][1]

    logged.clear()
    atoms.update(s="201", B=8)
    logger.access(
        cast(Response, SimpleNamespace(status="201 CREATED", sent=8)),
        req,
        _environ("/sub/api/api/state/polling"),
        timedelta(milliseconds=4),
    )
    assert logged[0][0] == 5

    logged.clear()
    atoms.update(s="500", B=8)
    logger.access(
        cast(Response, SimpleNamespace(status="500 ERROR", sent=8)),
        req,
        _environ("/sub/api/api/health"),
        timedelta(milliseconds=4),
    )
    assert logged[0][0] == logging.INFO


def test_access_log_colors_status_by_class(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[tuple[int, str]] = []
    logger = GunicornLogger.__new__(GunicornLogger)
    logger.cfg = cast(Any, _Cfg())
    logger.access_log = cast(Any, _AccessLog(logged))
    atoms: dict[str, object] = {"m": "GET", "H": "HTTP/1.1", "s": "200", "B": 4}

    def atoms_for(*args: object) -> dict[str, object]:
        del args
        return atoms

    monkeypatch.setattr(logger, "atoms", atoms_for)
    req = cast(Request, SimpleNamespace())

    expected = {
        "100": Colors.GREY,
        "204": Colors.GREEN,
        "302": Colors.CYAN,
        "404": Colors.YELLOW,
        "503": Colors.RED,
    }
    for status, color in expected.items():
        logged.clear()
        atoms["s"] = status
        logger.access(
            cast(Response, SimpleNamespace(status=status, sent=4)),
            req,
            _environ("/health"),
            timedelta(milliseconds=1),
        )
        assert [message for _, message in logged] == [
            f'203.0.113.10 > "GET /health HTTP/1.1" {color}{status}{Colors.RESET} 4b "dashboard"'
        ]

    assert _color_status("-") == "-"
    assert _color_status("999") == "999"


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

    with pytest.raises(ValueError):
        parse_level("")
    with pytest.raises(ValueError):
        parse_level("verbose")
    with pytest.raises(ValueError):
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


def test_gunicorn_access_level_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """LOGLEVEL_GUNICORN replaces gunicorn's hardcoded INFO access threshold.

    Error logs stay on gunicorn's own loglevel. A missing or bad value falls
    back to DEBUG so 2xx lines (and TRACE polls) are not silently dropped.
    """
    from gunicorn.config import Config

    error_log = logging.getLogger("gunicorn.error")
    access_log = logging.getLogger("gunicorn.access")
    previous = (error_log.level, access_log.level, list(error_log.handlers), list(access_log.handlers))

    def restore() -> None:
        error_log.setLevel(previous[0])
        access_log.setLevel(previous[1])
        error_log.handlers[:] = previous[2]
        access_log.handlers[:] = previous[3]

    cfg = Config()
    cfg.set("loglevel", "info")
    cfg.set("accesslog", None)
    cfg.set("errorlog", "-")

    try:
        monkeypatch.delenv("LOGLEVEL_GUNICORN", raising=False)
        logger = GunicornLogger(cfg)
        assert logger.access_log.level == logging.DEBUG
        assert logger.error_log.level == logging.INFO

        monkeypatch.setenv("LOGLEVEL_GUNICORN", "warning")
        logger.setup(cfg)
        assert logger.access_log.level == logging.WARNING
        assert logger.error_log.level == logging.INFO

        monkeypatch.setenv("LOGLEVEL_GUNICORN", "7")
        logger.setup(cfg)
        assert logger.access_log.level == TRACE

        monkeypatch.setenv("LOGLEVEL_GUNICORN", "nope")
        logger.setup(cfg)
        assert logger.access_log.level == logging.DEBUG
    finally:
        restore()


def test_jsonl_strips_message_colors() -> None:
    stored: list[Mapping[str, Any]] = []

    class _Lines:
        def append(self, record: Mapping[str, Any]) -> None:
            stored.append(record)

    handler = _JSONLinesLogger(cast(Any, _Lines()))
    logger = Logger("colors")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False

    logger.info(
        f"{Colors.BOLD}{Colors.ITALIC}{Colors.RED}Hello {Colors.GREEN}World!{Colors.RESET} regular"
    )

    assert stored[0]["text"] == "Hello World! regular"
