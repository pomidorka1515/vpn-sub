from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap_sys_path() -> None:
    here = Path(__file__).resolve().parent
    project_root = Path(__file__).resolve().parents[3]
    main_src = project_root / "src"
    for item in (str(main_src), str(here)):
        while item in sys.path:
            sys.path.remove(item)
    sys.path.insert(0, str(here))
    sys.path.insert(1, str(main_src))


_bootstrap_sys_path()

from runtime import (
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
