"""Shared runtime path resolution.

Locks and generation stamps must live on a directory this process can create.
``/run/lock`` is sticky and world-writable on Debian, but mode 0755 root:root
on RHEL, so a non-root service cannot create a file there. There is no FHS
directory that is sticky on every distro, and a predictable name under ``/tmp``
can be squatted. The data directory is already owned by the service user.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["bundled_root", "runtime_dir"]


def bundled_root() -> Path:
    """Directory that contains ``res/`` and ``lang.jsonc``.

    A normal checkout is the repository root, two levels above this module.
    Nuitka onefile extracts included data next to the compiled entry module,
    whose ``__file__`` is inside the payload rather than the checkout. Walk
    up from this module until those files are found, so both layouts resolve
    without an environment variable.

    ``DIR_DATA`` and ``PATH_*`` still override mutable runtime files. This
    only locates files that ship with the program.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "lang.jsonc").is_file() and (parent / "res").is_dir():
            return parent
    return here.parents[1]


def runtime_dir(data: Path | None = None) -> Path:
    """Directory for flock files and cross-process generation stamps.

    ``DIR_RUNTIME`` wins. Otherwise the directory is ``<DIR_DATA>/run``, and
    ``DIR_DATA`` itself defaults to ``<repo>/data``. ``data`` is the already
    resolved data directory; pass it so a caller that overrode ``DIR_DATA``
    does not read the environment a second time.

    The directory must be local. ``fcntl.flock`` is not reliable on NFS.
    """
    override = os.getenv("DIR_RUNTIME")
    if override:
        return Path(override)
    if data is None:
        root = bundled_root()
        data = Path(os.getenv("DIR_DATA", root / "data"))
    return data / "run"
