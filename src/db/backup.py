from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loggers import Logger


def instance_backup_dir(path: str, backup_dir: str) -> str:
    return str(Path(backup_dir) / Path(path).stem)


def do_backup(path: str, timeout: float, instance_dir: str, log: Logger) -> None:
    """Take an atomic SQLite snapshot in a per-instance backup directory."""
    Path(instance_dir).mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup_path = Path(instance_dir) / f"{timestamp}.sqlite3"
    fd, temporary = tempfile.mkstemp(dir=instance_dir, prefix=".tmp-", suffix=".sqlite3")
    os.close(fd)

    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    try:
        source = sqlite3.connect(path, timeout=timeout)
        destination = sqlite3.connect(temporary)
        source.backup(destination)
        destination.commit()
        destination.close()
        destination = None
        source.close()
        source = None
        Path(temporary).replace(backup_path)
    except Exception:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
        with contextlib.suppress(FileNotFoundError):
            Path(temporary).unlink()
        raise

    log.debug(f"backup saved: {backup_path}")


def prune_backups(instance_dir: str, retention: int, log: Logger) -> None:
    """Keep only the N most recent database backups."""
    files = sorted(Path(instance_dir).glob("*.sqlite3"))
    to_delete = files[:-retention]
    for path in to_delete:
        try:
            path.unlink()
            log.debug(f"pruned old backup: {path}")
        except OSError:
            log.exception("prune failed for %s", path)
