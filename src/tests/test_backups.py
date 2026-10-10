from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import TYPE_CHECKING, Literal, TextIO
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
from db.backup import do_backup, make_backup_thread, prune_backups
from loggers import Logger

if TYPE_CHECKING:
    from pathlib import Path


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
    real_open = open

    def fail_temp(path: str, mode: Literal["r", "w"] = "r", *, encoding: str | None = None) -> TextIO:
        if path.endswith(".tmp"):
            raise OSError("unwritable")
        return real_open(path, mode, encoding=encoding)

    with patch("config.backup.open", side_effect=fail_temp):
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
        patch("os.unlink", side_effect=OSError("busy")),
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
            path=str(source), indent=2, backup_dir=str(tmp_path / "out"),
            backup_interval=0, backup_retention=1, stop_event=stop,
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
            path=str(source), indent=2, backup_dir=str(tmp_path),
            backup_interval=5, backup_retention=1, stop_event=stop,
            config_type="jsonl", raw=True,
        ).run()
    error.assert_called()
    critical.assert_called()


def test_database_backup_roundtrip_and_prune(tmp_path: Path) -> None:
    source = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(source)
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
        patch("os.unlink", side_effect=OSError("busy")),
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


def test_database_backup_thread_retries(tmp_path: Path) -> None:
    calls = {"count": 0}

    def fail_once(*args: object, **kwargs: object) -> None:
        del args, kwargs
        calls["count"] += 1
        if calls["count"] < 3:
            raise sqlite3.OperationalError("locked")

    with (
        patch("db.backup.do_backup", side_effect=fail_once),
        patch("db.backup.prune_backups"),
        patch.object(threading.Event, "wait", side_effect=[False, False, False, True]),
        patch.object(Logger, "error") as error,
        patch.object(Logger, "critical") as critical,
    ):
        make_backup_thread(
            path=str(tmp_path / "state.sqlite3"), timeout=1,
            backup_dir=str(tmp_path), backup_interval=1, backup_retention=1,
            stop_event=threading.Event(),
        ).run()
    assert calls["count"] == 3
    error.assert_called()
    critical.assert_not_called()
    assert os.path.basename(str(tmp_path))
