"""Cross-process generation files. A changed mtime drops a worker's cache.

The file stores no inbound or client data. Two caches must not share a stamp:
a client write and an inbound write invalidate different maps.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Literal

from paths import runtime_dir

__all__ = ["bump_stamp", "inbound_stamp_path", "read_stamp", "stamp_path"]

StampKind = Literal["inbounds", "clients"]


def stamp_path(
    kind: StampKind,
    name: str,
    base_url: str,
    *,
    directory: str | None = None,
) -> str:
    """Per-panel generation file under ``directory``.

    ``name`` keeps two panels apart; the URL digest keeps a weird name from
    escaping ``directory``. ``directory`` defaults to the service runtime dir
    (``DIR_RUNTIME``, else ``<DIR_DATA>/run``).
    """
    if directory is None:
        directory = str(runtime_dir())
    digest = hashlib.sha1(base_url.encode(), usedforsecurity=False).hexdigest()[:8]
    safe = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in name) or "panel"
    return str(Path(directory) / f"{kind}.{safe}.{digest}.stamp")


def inbound_stamp_path(name: str, base_url: str, *, directory: str | None = None) -> str:
    """Inbound generation file. A changed mtime drops every worker's inbound cache."""
    return stamp_path("inbounds", name, base_url, directory=directory)


def client_stamp_path(name: str, base_url: str, *, directory: str | None = None) -> str:
    """Client-list generation file. Not the inbound stamp.

    Sharing one stamp would drop the client map on every inbound clear, and
    the other way around. The file stores no client rows.
    """
    return stamp_path("clients", name, base_url, directory=directory)


def bump_stamp(path: str) -> None:
    """Move the stamp's mtime. A same-nanosecond utime is pushed forward."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(path, flags, 0o644)
    try:
        previous = os.fstat(fd).st_mtime_ns
        os.utime(fd)
        current = os.fstat(fd).st_mtime_ns
        if current <= previous:
            os.utime(fd, ns=(current + 1, current + 1))
    finally:
        os.close(fd)


def read_stamp(path: str) -> int:
    """Current generation, creating the stamp when this worker is first.

    A missing file is not generation 0. Two workers can both observe "absent"
    and would then treat each other's later fills as still current.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0), 0o644)
    try:
        return os.fstat(fd).st_mtime_ns
    finally:
        os.close(fd)
