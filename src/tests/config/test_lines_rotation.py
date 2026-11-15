from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest

from config import LinesConfig

if TYPE_CHECKING:
    from pathlib import Path

    from config.constants import SYNC_MODES, JsonDict

MB = 1_000_000


@pytest.mark.parametrize("limit", [0, -1])
def test_rotation_rejects_nonpositive_limit(tmp_path: Path, limit: int) -> None:
    path = tmp_path / "audit.jsonl"
    with pytest.raises(ValueError, match="max_size_mb must be positive"):
        LinesConfig(path, max_size_mb=limit)
    assert not path.exists()


def test_default_limit_is_100_decimal_megabytes(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    with LinesConfig(path) as config:
        assert config.max_size_mb == 100
        # Sparse file: exercise the real default without allocating 100 MB.
        with path.open("wb") as file:
            file.truncate(100 * MB + 1)
        config.append({"event": "hit"})
        assert config.size == 0
        assert config.read_all() == []
        config.append({"event": "fresh"})
        assert config.read_all() == [{"event": "fresh"}]


@pytest.mark.parametrize("sync_mode", ["none", "data", "full"])
@pytest.mark.parametrize("many", [False, True])
def test_rotation_purges_at_byte_limit_before_durability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sync_mode: SYNC_MODES, many: bool
) -> None:
    path = tmp_path / "audit.jsonl"
    synced_sizes: list[int] = []
    parent_syncs: list[str] = []

    def fsync(fd: int) -> None:
        synced_sizes.append(path.stat().st_size)

    monkeypatch.setattr("config.lines.os.fsync", fsync)
    monkeypatch.setattr("config.lines.fsync_parent_dir", parent_syncs.append)
    with LinesConfig(path, sync_mode=sync_mode, max_size_mb=1) as config:
        overhead = len((json.dumps({"value": ""}) + "\n").encode())
        # Multibyte data: the threshold counts bytes, not Unicode characters.
        record: JsonDict = {"value": "é" * ((MB - overhead) // 2) + "x" * ((MB - overhead) % 2)}
        config.append(record)
        assert config.size == MB  # Equal to the limit is retained.
        assert config.read_all() == [record]
        synced_sizes.clear()
        parent_syncs.clear()
        if many:
            config.append_many([{"event": "hit"}, {"event": "another hit"}])
        else:
            config.append({"event": "hit"})
        assert config.size == 0
        assert config.read_all() == []
        assert synced_sizes == ([] if sync_mode == "none" else [0])
        assert parent_syncs == ([str(path)] if sync_mode == "full" else [])
        config.append({"event": "fresh"})
        assert config.read_all() == [{"event": "fresh"}]
    assert sorted(file.name for file in tmp_path.iterdir()) == ["audit.jsonl", "audit.jsonl.lock"]


def test_oversized_batch_is_fully_purged(tmp_path: Path) -> None:
    with LinesConfig(tmp_path / "audit.jsonl", max_size_mb=1) as config:
        config.append({"old": True})
        config.append_many([{"value": "x" * 600_000}, {"value": "y" * 600_000}])
        assert config.size == 0
        assert config.read_all() == []


def test_rotation_serializes_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    record: JsonDict = {"value": "x" * 40_000}
    record_size = len((json.dumps(record) + "\n").encode())
    with (
        LinesConfig(path, max_size_mb=1) as first,
        LinesConfig(path, max_size_mb=1) as second,
        ThreadPoolExecutor(max_workers=8) as workers,
    ):

        def append(index: int) -> None:
            (first if index % 2 else second).append(record)

        list(workers.map(append, range(64)))
        retained = 64 % (MB // record_size + 1)
        assert first.size == retained * record_size
        assert first.read_all() == [record] * retained
