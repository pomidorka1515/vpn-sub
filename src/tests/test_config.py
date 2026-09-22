from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import json

import pytest

from config import Config, LinesConfig
from errors import (
    ConfigError,
    FileCorruptionError,
    ReadOnlyConfigError,
    SchemaValidationError,
)


@pytest.fixture
def cfg(tmp_path: Path) -> Iterator[Config]:
    config = Config(path=tmp_path / "config.json", backup_dir=None, sync_mode="none")
    yield config
    config.close()


def test_creates_missing_file_as_empty_object(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    cfg = Config(path=path, backup_dir=None, sync_mode="none")
    try:
        assert path.is_file()
        assert cfg.copy() == {}
        assert json.loads(path.read_text(encoding="utf-8")) == {}
    finally:
        cfg.close()


def test_assignment_persists_to_disk(cfg: Config) -> None:
    cfg["domain"] = "https://example.test"
    assert cfg["domain"] == "https://example.test"
    assert json.loads(Path(cfg.path).read_text(encoding="utf-8"))["domain"] == (
        "https://example.test"
    )


def test_transaction_commit_writes_nested_changes(cfg: Config) -> None:
    with cfg as tx:
        tx["panel"] = {"name": "local"}
        tx["panel"]["port"] = 2053
    assert cfg["panel"] == {"name": "local", "port": 2053}


def test_transaction_exception_does_not_persist(cfg: Config) -> None:
    cfg["keep"] = 1
    with pytest.raises(ValueError, match="abort"):
        with cfg as tx:
            tx["keep"] = 2
            raise ValueError("abort")
    assert cfg["keep"] == 1


def test_isolate_commits_detaches_leaked_transaction_refs(tmp_path: Path) -> None:
    cfg = Config(
        path=tmp_path / "config.json",
        backup_dir=None,
        sync_mode="none",
        isolate_commits=True,
    )
    try:
        with cfg as tx:
            tx["nested"] = {"a": 1}
            leaked = tx["nested"]
        leaked["a"] = 2
        assert cfg["nested"]["a"] == 1
    finally:
        cfg.close()


def test_disabled_isolate_commits_keeps_leaked_transaction_refs(tmp_path: Path) -> None:
    cfg = Config(
        path=tmp_path / "config.json",
        backup_dir=None,
        sync_mode="none",
        isolate_commits=False,
    )
    try:
        with cfg as tx:
            tx["nested"] = {"a": 1}
            leaked = tx["nested"]
        leaked["a"] = 2
        assert cfg["nested"]["a"] == 2
    finally:
        cfg.close()


def test_direct_access_inside_transaction_is_rejected(cfg: Config) -> None:
    with cfg as tx:
        tx["x"] = 1
        with pytest.raises(RuntimeError, match="transaction"):
            _ = cfg["x"]


def test_nested_transactions_are_rejected(cfg: Config) -> None:
    with cfg.edit() as tx:
        tx["x"] = 1
        with pytest.raises(RuntimeError, match="Nested"):
            with cfg.edit() as inner:
                inner["x"] = 2


def test_reads_pick_up_external_file_changes(cfg: Config) -> None:
    cfg["x"] = 1
    Path(cfg.path).write_text(json.dumps({"x": 2, "extra": True}), encoding="utf-8")
    assert cfg["x"] == 2
    assert cfg["extra"] is True


def test_read_only_rejects_mutation(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    cfg = Config(path=path, read_only=True, backup_dir=None, sync_mode="none")
    try:
        with pytest.raises(ReadOnlyConfigError):
            cfg["x"] = 1
    finally:
        cfg.close()


def test_read_only_jsonc_requires_read_only(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="read_only_jsonc"):
        Config(
            path=tmp_path / "config.json",
            read_only_jsonc=True,
            backup_dir=None,
            sync_mode="none",
        )


def test_read_only_jsonc_strips_comments_and_trailing_commas(tmp_path: Path) -> None:
    path = tmp_path / "lang.jsonc"
    path.write_text(
        '{\n  // line\n  "foo": 1, /* block */\n  "bar": [1, 2,],\n}\n',
        encoding="utf-8",
    )
    cfg = Config(
        path=path,
        read_only=True,
        read_only_jsonc=True,
        backup_dir=None,
        sync_mode="none",
    )
    try:
        assert cfg.copy() == {"foo": 1, "bar": [1, 2]}
        assert "// line" in path.read_text(encoding="utf-8")
    finally:
        cfg.close()


def test_missing_jsonc_file_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / "missing.jsonc"
    with pytest.raises(FileNotFoundError):
        Config(
            path=path,
            read_only=True,
            read_only_jsonc=True,
            backup_dir=None,
            sync_mode="none",
        )
    assert not path.exists()


def test_corrupt_json_raises_file_corruption_error(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(FileCorruptionError):
        Config(path=path, backup_dir=None, sync_mode="none")


def test_top_level_non_object_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ConfigError, match="JSON object"):
        Config(path=path, backup_dir=None, sync_mode="none")


def test_remote_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"$schema": "https://example.test/schema.json"}),
        encoding="utf-8",
    )
    with pytest.raises(SchemaValidationError, match="Remote"):
        Config(path=path, backup_dir=None, sync_mode="none")


def test_local_schema_validation_error(tmp_path: Path) -> None:
    (tmp_path / "schema.json").write_text(
        json.dumps({
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
            "additionalProperties": True,
        }),
        encoding="utf-8",
    )
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"$schema": "schema.json"}), encoding="utf-8")
    with pytest.raises(SchemaValidationError, match="Schema validation"):
        Config(path=path, backup_dir=None, sync_mode="none")


def test_local_schema_accepts_valid_document(tmp_path: Path) -> None:
    (tmp_path / "schema.json").write_text(
        json.dumps({
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
            "additionalProperties": True,
        }),
        encoding="utf-8",
    )
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"$schema": "schema.json", "name": "ok"}),
        encoding="utf-8",
    )
    cfg = Config(path=path, backup_dir=None, sync_mode="none")
    try:
        assert cfg["name"] == "ok"
    finally:
        cfg.close()


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


def test_lines_append_tail_and_compact(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    lines = LinesConfig(path, backup_dir=None, sync_mode="none")
    try:
        lines.append({"id": 1, "keep": True})
        lines.append_many((
            {"id": 2, "keep": False},
            {"id": 3, "keep": True, "text": "привет"},
        ))
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
