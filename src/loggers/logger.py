from __future__ import annotations

import logging
import time
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from .colors import Colors
from .common import _ANSI_ESCAPE
from .handlers import _JSONLinesLogger, _TelegramLogger
from .level import TRACE, env_level

if TYPE_CHECKING:
    from bots import AdminBot
    from config import LinesConfigLike

__all__ = ["Logger", "TRACE"]


class Logger(logging.Logger):
    COLORS = {
        "TRACE": Colors.GREY,
        "DEBUG": Colors.CYAN,
        "INFO": Colors.GREEN,
        "WARNING": Colors.YELLOW,
        "ERROR": Colors.RED,
        "CRITICAL": Colors.MAGENTA,
    }
    RESET = Colors.RESET
    TRACE = TRACE

    def __init__(self, name: str, level: int | None = None):
        """Open a process logger.

        ``level`` overrides the threshold for this logger only. When it is
        omitted, ``LOGLEVEL`` applies (default DEBUG). Gunicorn access logs
        are not ``Logger`` instances and do not read that variable.
        """
        if level is None:
            level = env_level("LOGLEVEL", logging.DEBUG)
        super().__init__(name, level)
        logging.Logger.manager.loggerDict[name] = self
        self.ansi_escape = _ANSI_ESCAPE
        if not self.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(self._make_formatter())
            self.addHandler(handler)

    def trace(
        self,
        msg: object,
        *args: object,
        exc_info: Any = None,
        stack_info: bool = False,
        stacklevel: int = 1,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        """Log ``msg % args`` at TRACE, below DEBUG."""
        if self.isEnabledFor(TRACE):
            self._log(
                TRACE,
                msg,
                args,
                exc_info=exc_info,
                stack_info=stack_info,
                stacklevel=stacklevel,
                extra=extra,
            )

    def _make_formatter(self) -> logging.Formatter:
        parent = self
        class Fmt(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                orig_level = record.levelname
                color = parent.COLORS.get(record.levelname, Colors.RESET)
                record.levelname = f"{color}{record.levelname}{Colors.RESET}"
                val = super().format(record)
                record.levelname = orig_level
                return val
        return Fmt(
            fmt='%(asctime)s %(levelname)s [%(name)s] [%(threadName)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

    def set_tg_bot(self, bot: AdminBot, level: int | None = None) -> None:
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
            self.info(f"Loaded {Colors.BOLD}{self.name}{Colors.RESET}! {Colors.ITALIC}({dt:.1f}ms){Colors.RESET}")
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
            self.info(f"Executed: {name} {Colors.ITALIC}({dt:.1f}ms){Colors.RESET}")
        except Exception as e:
            self.error(f"Fail when executing {name}: {e}")
            raise
