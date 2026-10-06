"""Command-line flags for the release binaries.

``vpn-sub`` and ``vpn-sub-discord`` both call :func:`dispatch` before they
start. ``--help``, ``--probe``, ``--update``, and ``--load-scripts`` are the
same flags on both binaries. A checkout (``python -m main``, ``python -m
runtime``) accepts them too. With no flag, ``dispatch`` returns and the caller
starts the process.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from loggers import Logger

__all__ = ["dispatch"]


def dispatch(prog: str, probe: Callable[[], None], log: Logger) -> None:
    """Run a flag from ``sys.argv`` and exit, or return when there is none.

    ``probe`` is supplied by the caller. The two binaries ship different files,
    so the check cannot live here. Help, update, and script loading are shared.
    An unknown flag is not a flag: the caller starts as if argv were empty.
    """
    if len(sys.argv) <= 1:
        return
    flag = sys.argv[1]
    if flag in ("--help", "-h"):
        from cli.help import print_help

        print_help(prog)
        raise SystemExit(0)
    if flag == "--probe":
        probe()
        return
    if flag == "--update":
        from cli.updater import update

        raise SystemExit(update(log))
    if flag == "--load-scripts":
        from cli.scripts import load_scripts

        raise SystemExit(load_scripts(log))
