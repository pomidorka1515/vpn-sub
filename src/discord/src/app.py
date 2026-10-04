"""Checkout-only path bootstrap for the Discord service.

The process entry is ``runtime``. ``python -m runtime`` works in a checkout
when ``PYTHONPATH`` already lists ``src/discord/src`` before ``src``. A Nuitka
binary compiles ``runtime.py``, not this file. This module remains so a
checkout can still fix that path order. Do not compile it: the rewrite
assumes ``src/discord/src/app.py``, and that layout does not exist inside a
payload.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap_sys_path() -> None:
    """Prefer this package, then the main ``src`` tree.

    Only runs as a checkout script. ``__file__`` is then
    ``src/discord/src/app.py``, so the main tree is three parents up. A
    compiled module has no such layout. Nuitka does not set ``sys.frozen``;
    a compiled module has ``__compiled__``. Skip the rewrite in that case.
    ``runtime`` does not import this module, so the skip is only a guard for
    someone who compiles the wrong file.
    """
    if "__compiled__" in globals():
        return
    here = Path(__file__).resolve()
    if len(here.parents) < 4:
        return
    main_src = here.parents[3] / "src"
    if not (main_src / "paths.py").is_file():
        return
    package = str(here.parent)
    main = str(main_src)
    for item in (main, package):
        while item in sys.path:
            sys.path.remove(item)
    sys.path.insert(0, package)
    sys.path.insert(1, main)


_bootstrap_sys_path()

from runtime import (  # noqa: E402
    DiscordApplication,
    DiscordPaths,
    create_application,
    main,
)

__all__ = [
    "DiscordApplication",
    "DiscordPaths",
    "create_application",
    "main",
]


if __name__ == "__main__":
    main()
