from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

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


def make_backup_thread(
    *,
    path: str,
    timeout: float,
    backup_dir: str,
    backup_interval: float,
    backup_retention: int,
    stop_event: threading.Event,
) -> threading.Thread:
    def log_failure(log: Logger, failures: int, exc: Exception) -> None:
        if failures == 1:
            log.error(f"backup failed for {path}", exc_info=exc)
        elif failures % 3 == 0:
            log.critical(
                f"backup still failing for {path} after {failures} consecutive attempts",
                exc_info=exc,
            )

    def loop() -> None:
        log = Logger("Backup")
        instance_dir = instance_backup_dir(path, backup_dir)
        failures = 0
        while not stop_event.wait(backup_interval):
            try:
                do_backup(path, timeout, instance_dir, log)
                prune_backups(instance_dir, backup_retention, log)
                log.info("backup successful")
                failures = 0
                continue
            except Exception as exc:
                failures += 1
                log_failure(log, failures, exc)

            retry_interval = min(backup_interval, 60 * failures)
            if stop_event.wait(retry_interval):
                break
            try:
                do_backup(path, timeout, instance_dir, log)
                prune_backups(instance_dir, backup_retention, log)
                log.info("backup retry successful")
                failures = 0
                continue
            except Exception as retry_exc:
                failures += 1
                log_failure(log, failures, retry_exc)

    return threading.Thread(target=loop, daemon=True, name="Backup")
