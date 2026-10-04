from __future__ import annotations

import logging
from collections.abc import MutableMapping
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from gunicorn.config import Config
from gunicorn.glogging import Logger as GunicornBaseLogger
from gunicorn.http.message import Request
from gunicorn.http.wsgi import Response

from .logger import Logger

__all__ = [
    "GunicornLogger",
    "_client_address",
    "_color_status",
    "_request_path",
    "_safe_request_target",
    "_should_skip_access_log",
]


def _safe_request_target(raw_uri: str) -> str:
    """Return a request target without leaking query-string credentials.

    Subscription URLs carry a secret token in the query string.  Keep the
    route and a redacted token marker for useful diagnostics, while dropping
    every other parameter (for example ``lang``).
    """
    try:
        parsed = urlsplit(raw_uri)
    except ValueError:
        return raw_uri.split("?", 1)[0] or "/"

    path = parsed.path or "/"
    if any(key == "token" for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        return f"{path}?token=..."
    return path


def _client_address(environ: MutableMapping[str, object]) -> str:
    """Use the original client address when the app is behind a proxy."""
    forwarded = environ.get("HTTP_X_FORWARDED_FOR")
    if isinstance(forwarded, str) and forwarded.strip():
        return forwarded.split(",", 1)[0].strip()
    real_ip = environ.get("HTTP_X_REAL_IP")
    if isinstance(real_ip, str) and real_ip.strip():
        return real_ip.strip()
    address = environ.get("REMOTE_ADDR")
    return str(address) if address else "-"


def _request_path(environ: MutableMapping[str, object]) -> str:
    raw_uri = environ.get("RAW_URI")
    if not isinstance(raw_uri, str) or not raw_uri:
        raw_uri = str(environ.get("PATH_INFO") or "/")
    return _safe_request_target(raw_uri).split("?", 1)[0]


def _should_skip_access_log(environ: MutableMapping[str, object], status: str) -> bool:
    """Drop routine dashboard polls. Failures stay visible."""
    if status != "200":
        return False
    path = _request_path(environ).rstrip("/") or "/"
    return path == "/api/state/polling" or path.endswith("/api/state/polling")


def _color_status(status: str) -> str:
    """Color an HTTP status by class for journal access logs."""
    colors = {
        "1": "\033[90m",  # grey
        "2": "\033[32m",  # green
        "3": "\033[36m",  # cyan
        "4": "\033[33m",  # yellow
        "5": "\033[31m",  # red
    }
    color = colors.get(status[:1])
    if color is None:
        return status
    return f"{color}{status}{Logger.RESET}"


class GunicornLogger(GunicornBaseLogger):
    """Compact, privacy-preserving access logs for the systemd journal."""

    def setup(self, cfg: Config) -> None:
        super().setup(cfg)
        class AccessFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                original_level = record.levelname
                color = Logger.COLORS.get(original_level, Logger.RESET)
                record.levelname = f"{color}{original_level}{Logger.RESET}"
                try:
                    return super().format(record)
                finally:
                    record.levelname = original_level

        formatter = AccessFormatter(
            "%(asctime)s %(levelname)s [HTTP] [main] %(message)s", # [main] = "thread name", not relevant in this context
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        for handler in self.access_log.handlers:
            handler.setFormatter(formatter)

    def access(self, resp: Response, req: Request, environ: MutableMapping[str, Any], request_time: timedelta) -> None:
        if not self.access_log_enabled:
            return

        atoms = self.atoms(resp, req, environ, request_time)
        status = str(atoms.get("s") or "-")
        if _should_skip_access_log(environ, status):
            return

        method = str(atoms.get("m") or environ.get("REQUEST_METHOD") or "-")
        raw_uri = str(environ.get("RAW_URI") or environ.get("PATH_INFO") or "/")
        protocol = str(atoms.get("H") or environ.get("SERVER_PROTOCOL") or "HTTP/1.1")
        target = _safe_request_target(raw_uri)
        sent = atoms.get("B")
        size = f"{sent}b" if sent is not None else "-"
        user_agent = str(environ.get("HTTP_USER_AGENT") or "-").replace('"', "\\\"")
        message = f'{_client_address(environ)} > "{method} {target} {protocol}" {_color_status(status)} {size} "{user_agent}"'
        self.access_log.info(message)
