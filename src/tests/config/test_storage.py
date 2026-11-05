from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from config import Config, LinesConfig
from errors import ConfigError


def test_backup_now_requires_backup_dir(cfg: Config) -> None:
    with pytest.raises(ConfigError, match="backup_dir"):
        cfg.backup_now()


def test_invalid_sync_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sync_mode"):
        Config(
            path=tmp_path / "config.json",
            backup_dir=None,
            sync_mode="fast",  # type: ignore[arg-type]
        )


def _dir_lock_name(path: Path) -> str:
    digest = hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:8]
    return f"{path.name}.{digest}.lock"


def test_lockfile_path_accepts_directory_or_file(tmp_path: Path) -> None:
    data = tmp_path / "config.json"
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    by_dir = Config(path=data, backup_dir=None, sync_mode="none", lockfile_path=lock_dir)
    try:
        assert by_dir.lockfile_path == str(lock_dir / _dir_lock_name(data))
        by_dir["ok"] = True
        assert Path(by_dir.lockfile_path).is_file()
        assert not Path(f"{data}.lock").exists()
    finally:
        by_dir.close()

    other = tmp_path / "elsewhere" / "config.json"
    other.parent.mkdir()
    other_cfg = Config(path=other, backup_dir=None, sync_mode="none", lockfile_path=lock_dir)
    try:
        assert other_cfg.lockfile_path != by_dir.lockfile_path
        assert Path(other_cfg.lockfile_path).name == _dir_lock_name(other)
    finally:
        other_cfg.close()

    named = tmp_path / "custom" / "lock.file"
    by_file = Config(path=data, backup_dir=None, sync_mode="none", lockfile_path=named)
    try:
        assert by_file.lockfile_path == str(named)
        assert by_file["ok"] is True
        assert named.is_file()
    finally:
        by_file.close()

    log_path = tmp_path / "log.jsonl"
    lines = LinesConfig(
        log_path,
        backup_dir=None,
        sync_mode="none",
        lockfile_path=f"{lock_dir}{os.sep}",
    )
    try:
        lines.append({"id": 1})
        assert lines.lockfile_path == str(lock_dir / _dir_lock_name(log_path))
        assert Path(lines.lockfile_path).is_file()
    finally:
        lines.close()


def test_lines_append_tail_and_compact(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    lines = LinesConfig(path, backup_dir=None, sync_mode="none")
    try:
        lines.append({"id": 1, "keep": True})
        lines.append_many(
            (
                {"id": 2, "keep": False},
                {"id": 3, "keep": True, "text": "привет"},
            ),
        )
        assert lines.count() == 3
        assert lines.first(1) == [{"id": 1, "keep": True}]
        assert list(lines.tail(2)) == [
            {"id": 2, "keep": False},
            {"id": 3, "keep": True, "text": "привет"},
        ]
        result = lines.compact(lambda record: bool(record.get("keep")))
        assert result.kept == 2
        assert result.removed == 1
        assert lines.read_all() == [
            {"id": 1, "keep": True},
            {"id": 3, "keep": True, "text": "привет"},
        ]
        lines.clear()
        assert lines.count() == 0
        assert lines.read_all() == []
    finally:
        lines.close()
