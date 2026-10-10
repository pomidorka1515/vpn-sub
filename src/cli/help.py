"""Usage text for the release binaries.

``vpn-sub --help`` and ``vpn-sub-discord --help`` both call :func:`print_help`.
The flags are the same on both binaries. Printing happens before the service
starts, so a missing config or a down Redis is not a help failure. A checkout
(``python -m main``, ``python -m runtime``) accepts the same flag.
"""

from __future__ import annotations

from version import VERSION

__all__ = ["print_help", "usage"]

USAGE = """\
usage: {prog} [--help] [--probe] [--update] [--load-scripts]

VPN subscription service ({version}). With no flag, start the process.

  --help          show this usage and exit
  --probe         check that shipped files open, then exit
  --update        replace the installed binaries from the latest release
  --load-scripts  write the packed schema and download scripts/ beside the binary

--update and --load-scripts only run from a compiled binary. A checkout
already has those files and refuses. --probe does not start the service.
"""


def usage(prog: str) -> str:
    """Return the usage text for ``prog``."""
    return USAGE.format(prog=prog, version=VERSION)


def print_help(prog: str) -> None:
    """Write usage for ``prog`` to stdout and return.

    Does not exit. The caller raises ``SystemExit`` so the process code is
    explicit at the flag dispatch.
    """
    print(usage(prog), end="")  # noqa: T201
