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

__all__ = ["runtime_dir"]


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
        root = Path(__file__).resolve().parent.parent
        data = Path(os.getenv("DIR_DATA", root / "data"))
    return data / "run"
