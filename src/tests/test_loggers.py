from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gunicorn.http.message import Request
from gunicorn.http.wsgi import Response

from loggers import GunicornLogger
from loggers import _color_status


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


def test_access_log_skips_successful_state_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[str] = []
    logger = GunicornLogger.__new__(GunicornLogger)
    logger.cfg = cast(Any, _Cfg())
    logger.access_log = cast(Any, SimpleNamespace(info=logged.append))
    atoms: dict[str, object] = {"m": "GET", "H": "HTTP/1.1", "s": "200", "B": 12}
    monkeypatch.setattr(logger, "atoms", lambda *args: atoms)
    req = cast(Request, SimpleNamespace())

    logger.access(
        cast(Response, SimpleNamespace(status="200 OK", sent=12)),
        req,
        _environ("/sub/api/api/state/polling"),
        timedelta(milliseconds=4),
    )
    assert logged == []

    atoms.update(s="401", B=18)
    logger.access(
        cast(Response, SimpleNamespace(status="401 UNAUTHORIZED", sent=18)),
        req,
        _environ("/sub/api/api/state/polling"),
        timedelta(milliseconds=4),
    )
    assert len(logged) == 1
    assert "401" in logged[0]
    assert "/api/state/polling" in logged[0]

    logged.clear()
    atoms.update(s="200", B=8)
    logger.access(
        cast(Response, SimpleNamespace(status="200 OK", sent=8)),
        req,
        _environ("/sub/api/api/health"),
        timedelta(milliseconds=4),
    )
    assert len(logged) == 1
    assert "200" in logged[0]


def test_access_log_colors_status_by_class(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[str] = []
    logger = GunicornLogger.__new__(GunicornLogger)
    logger.cfg = cast(Any, _Cfg())
    logger.access_log = cast(Any, SimpleNamespace(info=logged.append))
    atoms: dict[str, object] = {"m": "GET", "H": "HTTP/1.1", "s": "200", "B": 4}
    monkeypatch.setattr(logger, "atoms", lambda *args: atoms)
    req = cast(Request, SimpleNamespace())

    expected = {
        "100": "\033[90m",
        "204": "\033[32m",
        "302": "\033[36m",
        "404": "\033[33m",
        "503": "\033[31m",
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
        assert logged == [
            f'203.0.113.10 > "GET /health HTTP/1.1" {color}{status}\033[0m 4b "dashboard"'
        ]

    assert _color_status("-") == "-"
    assert _color_status("999") == "999"
