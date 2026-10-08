from __future__ import annotations

__all__ = ["Colors", "color_status"]


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


def color_status(status: str) -> str:
    """Color an HTTP status by class, matching the gunicorn access logger.

    1xx is grey, 2xx green, 3xx cyan, 4xx yellow, and 5xx red. A non-numeric
    or empty status is returned unchanged.
    """
    colors = {
        "1": Colors.GREY,
        "2": Colors.GREEN,
        "3": Colors.CYAN,
        "4": Colors.YELLOW,
        "5": Colors.RED,
    }
    color = colors.get(status[:1])
    if color is None:
        return status
    return f"{color}{status}{Colors.RESET}"
