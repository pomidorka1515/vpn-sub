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


def test_access_log_levels_by_status_and_path(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[tuple[int, str]] = []
    logger = GunicornLogger.__new__(GunicornLogger)
    logger.cfg = cast(Any, _Cfg())
    logger.access_log = cast(Any, SimpleNamespace(log=lambda level, message: logged.append((level, message))))
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
    logger.access_log = cast(Any, SimpleNamespace(log=lambda level, message: logged.append((level, message))))
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
