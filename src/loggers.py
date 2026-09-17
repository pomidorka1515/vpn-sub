import logging
import sys
import re
import html
import time
from urllib.parse import parse_qsl, urlsplit

from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, TYPE_CHECKING

from gunicorn.glogging import Logger as GunicornBaseLogger  # type: ignore[import-untyped]
from protocols import AdminBotLike
if TYPE_CHECKING:
    from config import LinesConfigLike # just to be safe

_ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def _safe_handle_error(handler: logging.Handler, record: logging.LogRecord) -> None:
    try:
        handler.handleError(record)
    except Exception:
        print(f"logging handler {handler!r} failed while handling {record!r}", file=sys.stderr)

__all__ = ['Logger']

class _TelegramLogger(logging.Handler):
    def __init__(self, bot: AdminBotLike):
        """
        Logging handler to broadcast messages to a telegram bot (AdminBot).
        """
        super().__init__()
        self.bot = bot
        self.ansi_escape = _ANSI_ESCAPE

    def emit(self, record: logging.LogRecord, **kwargs: str) -> None:
        """
        Args:
            record: The record to broadcast.
            **kwargs: Extra keyword arguments to pass into AdminBot.msg.
        """
        try:
            msg_text = self.format(record)
            clean_text = self.ansi_escape.sub('', msg_text)
            safe_text = html.escape(clean_text)
            self.bot.msg(f"<code>{safe_text}</code>", **kwargs)
        except Exception:
            _safe_handle_error(self, record)

class _JSONLinesLogger(logging.Handler):
    def __init__(self, config: LinesConfigLike):
        """
        Logging handler to write logs into a .jsonl file.
        Uses LinesConfig manager.
        """
        super().__init__()
        self.cfg = config
        self.ansi_escape = _ANSI_ESCAPE

    def emit(self, record: logging.LogRecord) -> None:
        """
        Args:
            record: The record to broadcast.
        """
        try:
            timestamp = record.created
            date = datetime.fromtimestamp(timestamp).strftime("%d.%m.%Y %H:%M:%S")
            level = record.levelname
            log_name = record.name
            thread_name = record.threadName
            text = record.getMessage()

            to_log: dict[str, str | float | None] = {
                "ts": timestamp,
                "date": date,
                "level": level,
                "name": log_name,
                "threadname": thread_name,
                "text": text
            }
            
            self.cfg.append(record=to_log)
        except Exception:
            _safe_handle_error(self, record)

class Logger(logging.Logger):
    COLORS = {
        'DEBUG': '\033[36m',
        'INFO': '\033[32m',
        'WARNING': '\033[33m',
        'ERROR': '\033[31m',
        'CRITICAL': '\033[35m',
    }
    RESET = '\033[0m'

    def __init__(self, name: str, level: int = logging.DEBUG):
        super().__init__(name, level)
        self.ansi_escape = _ANSI_ESCAPE
        if not self.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(self._make_formatter())
            self.addHandler(handler)
        
    def _make_formatter(self) -> logging.Formatter:
        parent = self
        class Fmt(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                orig_level = record.levelname
                color = parent.COLORS.get(record.levelname, parent.RESET)
                record.levelname = f"{color}{record.levelname}{parent.RESET}"
                val = super().format(record)
                record.levelname = orig_level
                return val
        return Fmt(
            fmt='%(asctime)s %(levelname)s [%(name)s] [%(threadName)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    
    def set_tg_bot(self, bot: AdminBotLike, level: int | None = None) -> None:
        self.handlers = [h for h in self.handlers if not isinstance(h, _TelegramLogger)]
        tg_handler = _TelegramLogger(bot)
        if level is None: tg_handler.setLevel(logging.WARNING) 
        simple_fmt = logging.Formatter('%(levelname)s [%(name)s] %(message)s')
        tg_handler.setFormatter(simple_fmt)
        self.addHandler(tg_handler)

    def set_jsonl_handler(self, lines_config: LinesConfigLike, level: int | None = None) -> None:
        self.handlers = [h for h in self.handlers if not isinstance(h, _JSONLinesLogger)]
        jsonl_handler = _JSONLinesLogger(lines_config)
        if level is None: jsonl_handler.setLevel(logging.INFO)
        self.addHandler(jsonl_handler)
    
    @contextmanager
    def loading(self) -> Generator[None, None, None]:
        self.debug(f"Loading {self.name}...")
        t0 = time.monotonic()
        try:
            yield
            dt = (time.monotonic() - t0) * 1000
            self.info(f"Loaded {self.name}! ({dt:.1f}ms)")
        except Exception:
            self.error(f"Failed to load {self.name}.")
            raise

    @contextmanager
    def span(self, name: str, verbose: bool = False) -> Generator[None, None, None]:
        if verbose: self.debug(f"Executing: {name}")
        t0 = time.monotonic()
        try:
            yield
            dt = (time.monotonic() - t0) * 1000
            self.info(f"Executed: {name} ({dt:.1f}ms)")
        except Exception as e:
            self.error(f"Fail when executing {name}: {e}")
            raise


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


def _client_address(environ: dict[str, object]) -> str:
    """Use the original client address when the app is behind a proxy."""
    forwarded = environ.get("HTTP_X_FORWARDED_FOR")
    if isinstance(forwarded, str) and forwarded.strip():
        return forwarded.split(",", 1)[0].strip()
    real_ip = environ.get("HTTP_X_REAL_IP")
    if isinstance(real_ip, str) and real_ip.strip():
        return real_ip.strip()
    address = environ.get("REMOTE_ADDR")
    return str(address) if address else "-"


class GunicornLogger(GunicornBaseLogger):  # type: ignore[misc]
    """Compact, privacy-preserving access logs for the systemd journal."""

    def setup(self, cfg: Any) -> None:
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

    def access(self, resp: Any, req: Any, environ: Any, request_time: Any) -> None:
        if not self.access_log_enabled:
            return

        atoms = self.atoms(resp, req, environ, request_time)
        method = str(atoms.get("m") or environ.get("REQUEST_METHOD") or "-")
        raw_uri = str(environ.get("RAW_URI") or environ.get("PATH_INFO") or "/")
        protocol = str(atoms.get("H") or environ.get("SERVER_PROTOCOL") or "HTTP/1.1")
        target = _safe_request_target(raw_uri)
        status = str(atoms.get("s") or "-")
        sent = atoms.get("B")
        size = f"{sent}b" if sent is not None else "-"
        user_agent = str(environ.get("HTTP_USER_AGENT") or "-").replace('"', "\\\"")
        message = f'{_client_address(environ)} > "{method} {target} {protocol}" {status} {size} "{user_agent}"'
        self.access_log.info(message)
