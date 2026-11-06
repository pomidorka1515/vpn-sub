from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from backuputil import publish_backup

from .schema import SCHEMA_STATEMENTS, SchemaMixin

if TYPE_CHECKING:
    from loggers import Logger


def instance_backup_dir(path: str, backup_dir: str) -> str:
    source = Path(path).resolve()
    identity = hashlib.sha256(os.fsencode(source)).hexdigest()
    return str(Path(backup_dir) / f"db-{identity}")


def _validate_snapshot(snapshot: sqlite3.Connection) -> None:
    if snapshot.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise sqlite3.DatabaseError("backup snapshot failed integrity check")

    row = snapshot.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    if row is None or not isinstance(row[0], int) or not 1 <= row[0] <= SchemaMixin.SCHEMA_VERSION:
        raise sqlite3.DatabaseError("backup snapshot has an invalid schema version")
    version = row[0]

    # Derive the current table/column contract from the schema used at startup.
    # Earlier versions lack columns added by the corresponding migrations.
    with contextlib.closing(sqlite3.connect(":memory:")) as reference:
        for statement in SCHEMA_STATEMENTS:
            reference.execute(statement)
        tables = reference.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        actual_tables = {
            row[0]
            for row in snapshot.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for (table,) in tables:
            if table not in actual_tables:
                raise sqlite3.DatabaseError(f"backup snapshot is missing table {table}")
            expected = {
                row[1] for row in reference.execute("SELECT * FROM pragma_table_info(?)", (table,))
            }
            if table == "users":
                if version < 3:
                    expected.discard("auth_token")
                if version < 5:
                    expected.discard("auth_token_expires_at")
                if version < 6:
                    expected.difference_update({"admin_disabled", "admin_disabled_wl"})
            if table == "bandwidth_snapshots" and version < 7:
                expected.discard("start_ts")
            actual = {
                row[1] for row in snapshot.execute("SELECT * FROM pragma_table_info(?)", (table,))
            }
            if missing := expected - actual:
                raise sqlite3.DatabaseError(
                    f"backup snapshot is missing columns in {table}: {', '.join(sorted(missing))}",
                )


def do_backup(path: str, timeout: float, instance_dir: str, log: Logger) -> None:
    """Take an atomic SQLite snapshot in a per-instance backup directory."""
    Path(instance_dir).mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=instance_dir, prefix=".tmp-", suffix=".sqlite3")
    os.close(fd)

    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    try:
        source = sqlite3.connect(
            f"{Path(path).absolute().as_uri()}?mode=ro", timeout=timeout, uri=True
        )
        destination = sqlite3.connect(temporary)
        source.backup(destination)
        destination.commit()
        _validate_snapshot(destination)
        destination.close()
        destination = None
        source.close()
        source = None
        backup_path = publish_backup(temporary, instance_dir, ".sqlite3")
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
