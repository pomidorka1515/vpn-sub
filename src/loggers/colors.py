from __future__ import annotations

__all__ = ["Colors"]


class Colors:
    """ANSI color codes for log messages.

    Values are strings, so they expand inside f-strings::

        log.info(f"{Colors.RED}Hello {Colors.GREEN}World!{Colors.RESET}")
    """

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"
    REVERSE = "\033[7m"
    STRIKE = "\033[9m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GREY = "\033[90m"
