from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TextIO
from unittest.mock import patch

import pytest

from config.backup import (
    do_backup as config_do_backup,
)
from config.backup import (
    make_backup_thread as config_make_backup_thread,
)
from config.backup import (
    prune_backups as config_prune_backups,
)
from db import Database
from db.backup import do_backup, prune_backups
from db.schema import SCHEMA_STATEMENTS, SchemaMixin
from loggers import Logger

type BackupKind = Literal["json", "jsonl", "sqlite3"]


def _initialize_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (id INTEGER PRIMARY KEY, version INTEGER NOT NULL)"
    )
    if connection.execute("SELECT 1 FROM schema_version").fetchone() is None:
        connection.execute(
            "INSERT INTO schema_version VALUES (1, ?)", (SchemaMixin.SCHEMA_VERSION,)
        )
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)


def _snapshot(kind: BackupKind, source: Path, instance: Path, value: int) -> None:
    if kind == "sqlite3":
        connection = sqlite3.connect(source)
        try:
            _initialize_schema(connection)
            connection.execute("CREATE TABLE IF NOT EXISTS items (value INTEGER)")
            connection.execute("DELETE FROM items")
            connection.execute("INSERT INTO items VALUES (?)", (value,))
            connection.commit()
        finally:
            connection.close()
        do_backup(str(source), 1, str(instance), _log())
    else:
        source.write_text(json.dumps({"value": value}) + "\n", encoding="utf-8")
        config_do_backup(str(source), 2, str(instance), _log(), raw=kind == "jsonl")


def _snapshot_value(kind: BackupKind, path: Path) -> int:
    if kind == "sqlite3":
        connection = sqlite3.connect(path)
        try:
            row = connection.execute("SELECT value FROM items").fetchone()
            assert row is not None
            return int(row[0])
        finally:
            connection.close()
    return int(json.loads(path.read_text(encoding="utf-8"))["value"])


def _prune(kind: BackupKind, instance: Path) -> None:
    if kind == "sqlite3":
        prune_backups(str(instance), 2, _log())
    else:
        config_prune_backups(str(instance), 2, _log(), kind)


@pytest.mark.parametrize("kind", ["json", "jsonl", "sqlite3"])
def test_same_second_backups_preserve_versions_and_retention(
    tmp_path: Path,
    kind: BackupKind,
) -> None:
    instance = tmp_path / "backups"
    instance.mkdir()
    legacy = instance / f"20260101-000000.{kind}"
    legacy.write_bytes(b"legacy")
    with patch("backuputil.datetime") as clock:
        clock.now.return_value = datetime(2026, 1, 1, tzinfo=UTC)
        for value in range(3):
            _snapshot(kind, tmp_path / f"source.{kind}", instance, value)
        saved = sorted(instance.glob(f"*.{kind}"))
        assert saved[0] == legacy
        assert legacy.read_bytes() == b"legacy"
        assert [_snapshot_value(kind, path) for path in saved[1:]] == [0, 1, 2]
        _prune(kind, instance)
        # Retention must not cause a later snapshot to reuse an older sequence.
        _snapshot(kind, tmp_path / f"source.{kind}", instance, 3)
        _prune(kind, instance)
    saved = sorted(instance.glob(f"*.{kind}"))
    assert [_snapshot_value(kind, path) for path in saved] == [2, 3]
    assert list(instance.glob(".tmp-*")) == []


@pytest.mark.parametrize("kind", ["json", "jsonl", "sqlite3"])
def test_concurrent_same_second_backups_do_not_replace_snapshots(
    tmp_path: Path,
    kind: BackupKind,
) -> None:
    instance = tmp_path / "backups"
    barrier = threading.Barrier(4)
    local = threading.local()
    real_link = Path.hardlink_to

    def racing_link(path: Path, target: str) -> None:
        if not getattr(local, "ready", False):
            local.ready = True
            barrier.wait(timeout=10)
        real_link(path, target)

    def snapshot(value: int) -> None:
        _snapshot(kind, tmp_path / f"source-{value}.{kind}", instance, value)

    with (
        patch("backuputil.datetime") as clock,
        patch.object(Path, "hardlink_to", autospec=True, side_effect=racing_link),
        ThreadPoolExecutor(max_workers=4) as executor,
    ):
        clock.now.return_value = datetime(2026, 1, 1, tzinfo=UTC)
        list(executor.map(snapshot, range(4)))
    saved = list(instance.glob(f"*.{kind}"))
    assert len(saved) == 4
    assert {_snapshot_value(kind, path) for path in saved} == set(range(4))
    assert list(instance.glob(".tmp-*")) == []


@pytest.mark.parametrize("kind", ["json", "jsonl", "sqlite3"])
def test_backup_publication_failure_cleans_temporary_snapshot(
    tmp_path: Path,
    kind: BackupKind,
) -> None:
    instance = tmp_path / "backups"
    with (
        patch.object(Path, "hardlink_to", side_effect=OSError("publication failed")),
        pytest.raises(OSError, match="publication failed"),
    ):
        _snapshot(kind, tmp_path / f"source.{kind}", instance, 1)
    assert list(instance.iterdir()) == []


def _log() -> Logger:
    return Logger("test-backup")


def test_config_backup_normalizes_json_and_jsonc(tmp_path: Path) -> None:
    source = tmp_path / "config.json"
    source.write_text('{"name": "vpn", "port": 1,}', encoding="utf-8")
    instance = tmp_path / "backups"
    log = _log()

    config_do_backup(str(source), 2, str(instance), log, jsonc=True)
    saved = list(instance.glob("*.json"))
    assert len(saved) == 1
    assert json.loads(saved[0].read_text(encoding="utf-8")) == {"name": "vpn", "port": 1}

    missing = tmp_path / "missing.json"
    config_do_backup(str(missing), 2, str(instance), log)
    assert len(list(instance.glob("*.json"))) == 1

    source.write_text("{not json", encoding="utf-8")
    with patch.object(Logger, "warning") as warning:
        config_do_backup(str(source), 2, str(instance), log, jsonc=True)
    warning.assert_called_once()
    config_do_backup(str(source), 2, str(instance), log)
    assert len(list(instance.glob("*.json"))) == 1


def test_config_raw_backup_copies_jsonl_and_cleans_temp(tmp_path: Path) -> None:
    source = tmp_path / "audit.jsonl"
    source.write_text('{"event": "hit"}\n', encoding="utf-8")
    instance = tmp_path / "raw"
    config_do_backup(str(source), 2, str(instance), _log(), raw=True, minify=True)
    saved = list(instance.glob("*.jsonl"))
    assert saved[0].read_text(encoding="utf-8") == '{"event": "hit"}\n'

    missing = tmp_path / "gone.jsonl"
    config_do_backup(str(missing), 2, str(instance), _log(), raw=True)
    assert list(instance.glob(".tmp-*")) == []

    with patch("config.backup.shutil.copy2", side_effect=OSError("disk")):
        with pytest.raises(OSError, match="disk"):
            config_do_backup(str(source), 2, str(instance), _log(), raw=True)
    assert list(instance.glob(".tmp-*")) == []


def test_config_backup_write_failure_removes_temp_file(tmp_path: Path) -> None:
    source = tmp_path / "config.json"
    source.write_text("{}", encoding="utf-8")
    instance = tmp_path / "json"
    real_open = Path.open

    def fail_temp(
        path: Path,
        mode: Literal["r", "w"] = "r",
        *,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> TextIO:
        if path.suffix == ".tmp":
            raise OSError("unwritable")
        return real_open(path, mode, encoding=encoding, errors=errors, newline=newline)

    with patch.object(Path, "open", autospec=True, side_effect=fail_temp):
        with pytest.raises(OSError, match="unwritable"):
            config_do_backup(str(source), 2, str(instance), _log(), minify=True)
    assert list(instance.glob(".tmp-*")) == []


def test_config_prune_keeps_newest_and_logs_unlink_errors(tmp_path: Path) -> None:
    instance = tmp_path / "prune"
    instance.mkdir()
    names = ["20260101-000001.json", "20260101-000002.json", "20260101-000003.json"]
    for name in names:
        (instance / name).write_text("{}", encoding="utf-8")
    (instance / "notes.txt").write_text("keep", encoding="utf-8")
    log = _log()
    config_prune_backups(str(instance), 1, log, "json")
    assert [path.name for path in instance.glob("*.json")] == ["20260101-000003.json"]
    assert (instance / "notes.txt").is_file()

    (instance / "20260101-000004.json").write_text("{}", encoding="utf-8")
    with (
        patch.object(Path, "unlink", side_effect=OSError("busy")),
        patch.object(log, "error") as error,
    ):
        config_prune_backups(str(instance), 1, log, "json")
    error.assert_called()
    assert (instance / "20260101-000003.json").is_file()


def test_config_backup_thread_retries_then_succeeds(tmp_path: Path) -> None:
    source = tmp_path / "config.json"
    source.write_text('{"ok": true}', encoding="utf-8")
    calls = {"count": 0}

    def fail_once(*args: object, **kwargs: object) -> None:
        del args, kwargs
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("locked")

    stop = threading.Event()
    waits = iter((False, False, True))

    def wait(timeout: float | None = None) -> bool:
        del timeout
        return next(waits)

    with (
        patch("config.backup.do_backup", side_effect=fail_once),
        patch("config.backup.prune_backups"),
        patch.object(threading.Event, "wait", staticmethod(wait)),
    ):
        thread = config_make_backup_thread(
            path=str(source),
            indent=2,
            backup_dir=str(tmp_path / "out"),
            backup_interval=0,
            backup_retention=1,
            stop_event=stop,
            config_type="json",
        )
        thread.run()
    assert calls["count"] == 2


def test_config_backup_thread_logs_repeated_failures(tmp_path: Path) -> None:
    source = tmp_path / "config.json"
    source.write_text("{}", encoding="utf-8")
    stop = threading.Event()
    waits = iter((False, False, False, True))

    def wait(timeout: float | None = None) -> bool:
        del timeout
        return next(waits)

    with (
        patch("config.backup.do_backup", side_effect=OSError("full")),
        patch.object(threading.Event, "wait", staticmethod(wait)),
        patch.object(Logger, "error") as error,
        patch.object(Logger, "critical") as critical,
    ):
        config_make_backup_thread(
            path=str(source),
            indent=2,
            backup_dir=str(tmp_path),
            backup_interval=5,
            backup_retention=1,
            stop_event=stop,
            config_type="jsonl",
            raw=True,
        ).run()
    error.assert_called()
    critical.assert_called()


def test_database_backup_roundtrip_and_prune(tmp_path: Path) -> None:
    source = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(source)
    _initialize_schema(connection)
    connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    connection.execute("INSERT INTO items (name) VALUES ('alice')")
    connection.commit()
    connection.close()

    instance = tmp_path / "db-backups"
    do_backup(str(source), 1, str(instance), _log())
    saved = list(instance.glob("*.sqlite3"))
    assert len(saved) == 1
    copied = sqlite3.connect(saved[0])
    assert copied.execute("SELECT name FROM items").fetchone() == ("alice",)
    copied.close()

    for index in range(3):
        path = instance / f"2026010{index}-000000.sqlite3"
        path.write_bytes(b"old")
    prune_backups(str(instance), 2, _log())
    remaining = sorted(path.name for path in instance.glob("*.sqlite3"))
    assert len(remaining) == 2
    assert "20260100-000000.sqlite3" not in remaining

    log = _log()
    with (
        patch.object(Path, "unlink", side_effect=OSError("busy")),
        patch.object(log, "error") as error,
    ):
        prune_backups(str(instance), 1, log)
    error.assert_called()
    assert len(list(instance.glob("*.sqlite3"))) == 2


def test_database_backup_closes_connections_on_failure(tmp_path: Path) -> None:
    source = tmp_path / "missing-dir" / "state.sqlite3"
    instance = tmp_path / "failed"
    with pytest.raises(sqlite3.OperationalError):
        do_backup(str(source), 0.1, str(instance), _log())
    assert list(instance.glob(".tmp-*")) == []


@pytest.mark.asyncio
async def test_missing_database_does_not_publish_or_prune_backups(tmp_path: Path) -> None:
    source = tmp_path / "state.sqlite3"
    database = Database(
        path=source, backup_dir=tmp_path / "backups", backup_retention=2, start_backup=False
    )
    await database.start()
    try:
        await database.set_metadata("saved", "value")
        await database.backup_now()
        await database.backup_now()
        instance = tmp_path / "backups" / "state"
        previous = {path: path.read_bytes() for path in instance.glob("*.sqlite3")}
        assert len(previous) == 2
        database.backup_retention = 1
        source.unlink()
        with pytest.raises(sqlite3.OperationalError):
            await database.backup_now()
        assert not source.exists()
        assert {path: path.read_bytes() for path in instance.iterdir()} == previous
    finally:
        await database.close()


@pytest.mark.parametrize(
    "invalid",
    ["empty", "unrelated", "missing_table", "missing_column", "version", "integrity", "corrupt"],
)
def test_invalid_database_is_not_published(tmp_path: Path, invalid: str) -> None:
    source = tmp_path / "source.sqlite3"
    if invalid == "corrupt":
        source.write_bytes(b"not a database")
    else:
        with sqlite3.connect(source) as connection:
            if invalid == "unrelated":
                connection.execute("CREATE TABLE other (id INTEGER)")
            elif invalid != "empty":
                _initialize_schema(connection)
                if invalid == "missing_table":
                    connection.execute("DROP TABLE codes")
                elif invalid == "missing_column":
                    connection.execute("ALTER TABLE users DROP COLUMN displayname")
                elif invalid == "version":
                    connection.execute("UPDATE schema_version SET version = 0")
                elif invalid == "integrity":
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    connection.execute(
                        "INSERT INTO codes(code, action, uses) VALUES ('bad', 'invalid', 1)"
                    )
    instance = tmp_path / "backups"
    with pytest.raises(sqlite3.DatabaseError):
        do_backup(str(source), 1, str(instance), _log())
    assert list(instance.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("version", range(1, 8))
async def test_supported_schema_snapshot_can_be_restored(tmp_path: Path, version: int) -> None:
    source = tmp_path / "source ?#% ü.sqlite3"
    database = Database(path=source, start_backup=False)
    await database.start()
    await database.set_metadata("saved", "value")
    await database.close()
    with sqlite3.connect(source) as connection:
        if version < 7:
            connection.execute("ALTER TABLE bandwidth_snapshots DROP COLUMN start_ts")
        if version < 6:
            connection.execute("ALTER TABLE users DROP COLUMN admin_disabled")
            connection.execute("ALTER TABLE users DROP COLUMN admin_disabled_wl")
        if version < 5:
            connection.execute("ALTER TABLE users DROP COLUMN auth_token_expires_at")
        if version < 3:
            connection.execute("DROP TABLE users")
            statement = SCHEMA_STATEMENTS[0].replace("        auth_token TEXT UNIQUE,\n", "")
            statement = statement.replace("        auth_token_expires_at INTEGER,\n", "")
            statement = statement.replace(
                "        admin_disabled INTEGER NOT NULL DEFAULT 0 CHECK (admin_disabled IN (0, 1)),\n",
                "",
            )
            statement = statement.replace(
                "        admin_disabled_wl INTEGER NOT NULL DEFAULT 0 CHECK (admin_disabled_wl IN (0, 1)),\n",
                "",
            )
            connection.execute(statement)
        connection.execute("UPDATE schema_version SET version = ?", (version,))
    instance = tmp_path / "backups"
    do_backup(str(source), 1, str(instance), _log())
    restored = Database(path=next(instance.glob("*.sqlite3")), start_backup=False)
    await restored.start()
    try:
        assert await restored.get_metadata("saved") == "value"
    finally:
        await restored.close()
