from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest

from config import Config
from errors import ConfigError, FileCorruptionError, ReadOnlyConfigError

if TYPE_CHECKING:
    from config import AppConfig, JsonDict


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


def test_reads_pick_up_external_file_changes(cfg: Config) -> None:
    cfg["x"] = 1
    Path(cfg.path).write_text(json.dumps({"x": 2, "extra": True}), encoding="utf-8")
    assert cfg["x"] == 2
    assert cfg["extra"] is True


def test_view_is_detached_typed_document(app_cfg: Config[AppConfig]) -> None:
    snapshot: AppConfig = app_cfg.view()
    assert type(snapshot) is dict
    if "bot" not in snapshot:
        raise AssertionError("fixture must include bot")
    token: str = snapshot["bot"]["token"]
    mode: Literal["whitelist", "blacklist"] = snapshot["3xui"]["local"]["mode"]
    optional: str | None = snapshot.get("api_uri")
    assert (token, mode, optional) == ("test", "whitelist", None)
    before = Path(app_cfg.path).read_bytes()
    snapshot["bot"]["whitelist"].append(2)
    snapshot["3xui"]["local"]["name"] = "changed"
    fresh = app_cfg.view()
    assert "bot" in fresh
    assert fresh["bot"]["whitelist"] == [1]
    assert fresh["3xui"]["local"]["name"] == "local"
    assert Path(app_cfg.path).read_bytes() == before
    copied: JsonDict = app_cfg.copy()
    assert copied == fresh


def test_view_reloads_external_changes(app_cfg: Config[AppConfig]) -> None:
    snapshot = app_cfg.view()
    snapshot["domain"] = "changed.example.test"
    Path(app_cfg.path).write_text(json.dumps(snapshot), encoding="utf-8")
    assert app_cfg.view()["domain"] == "changed.example.test"


def test_get_keeps_mapping_semantics(cfg: Config) -> None:
    cfg["value"] = {"items": [1]}
    default = object()
    assert cfg.get("missing") is None
    assert cfg.get("missing", default) is default
    snapshot = cfg.get("value", default)
    assert isinstance(snapshot, dict)
    snapshot["items"] = [1, 2]
    assert cfg["value"] == {"items": [1]}
    with cfg.edit() as tx:
        assert tx.get("missing") is None
        assert tx.get("missing", default) is default
        live = tx.get("value", default)
        assert isinstance(live, dict)
        live["items"] = [1, 2]
    assert cfg["value"] == {"items": [1, 2]}


def test_mapping_defaults_preserve_existing_value_types(cfg: Config) -> None:
    cfg["value"] = {"items": [1]}
    assert cfg.setdefault("value", "fallback") == {"items": [1]}
    assert cfg.pop("value", 0) == {"items": [1]}
    assert cfg.pop("missing", 0) == 0
    with cfg.edit() as tx:
        tx["value"] = [1, 2]
        assert tx.setdefault("value", "fallback") == [1, 2]
        assert tx.pop("value", False) == [1, 2]
        assert tx.setdefault("missing", "fallback") == "fallback"


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
