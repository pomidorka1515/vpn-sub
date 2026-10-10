from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import pytest

from config import AppConfig, Config, JsonDict, JsonValue, LinesConfig
from config.transaction import ConfigTransaction
from errors import (
    ConfigError,
    FileCorruptionError,
    ReadOnlyConfigError,
    SchemaValidationError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


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
        panel = tx["panel"]
        assert isinstance(panel, dict)
        panel["port"] = 2053
    assert cfg["panel"] == {"name": "local", "port": 2053}


def test_transaction_exception_does_not_persist(cfg: Config) -> None:
    cfg["keep"] = 1

    def abort_transaction() -> None:
        with cfg as tx:
            tx["keep"] = 2
            raise ValueError("abort")

    with pytest.raises(ValueError, match="abort"):
        abort_transaction()
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
            assert isinstance(leaked, dict)
        leaked["a"] = 2
        assert cfg["nested"] == {"a": 1}
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
            assert isinstance(leaked, dict)
        leaked["a"] = 2
        assert cfg["nested"] == {"a": 2}
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


@pytest.fixture
def app_cfg(tmp_path: Path) -> Iterator[Config[AppConfig]]:
    document: AppConfig = {
        "uri": "sub", "api_token": "test", "provider_id": "test", "salt": "test",
        "domain": "example.test", "ping_check_url": "https://example.test/ping",
        "sub_name": "test", "api_admin_ui_auth": ["admin", "password"],
        "fingerprints": ["chrome"], "bypass_packages": [], "panel_alert_cooldown": 3600,
        "nodes": {}, "profiles": {}, "redis": {"url": "redis://localhost/0"},
        "json_template": {
            "dns": {}, "routing": {"rules": [], "domainStrategy": "AsIs"},
            "inbounds": [], "outbounds": [], "remarks": "test",
        },
        "3xui": {"local": {
            "name": "local", "address": "localhost", "port": 2053, "uri": "panel",
            "token": "test-token-at-least-20-characters", "https": False,
            "whitelist": False, "inbounds_list": [1], "mode": "whitelist",
        }},
        "bot": {"token": "test", "whitelist": [1]},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    config = Config[AppConfig](
        path=path, schema_path=Path(__file__).resolve().parents[2] / "config.schema.json",
        backup_dir=None, sync_mode="none",
    )
    try:
        yield config
    finally:
        config.close()


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


def test_transaction_view_is_active_and_commits_nested_edits(app_cfg: Config[AppConfig]) -> None:
    transaction = ConfigTransaction(app_cfg)
    with pytest.raises(RuntimeError, match="not active"):
        transaction.view()
    with transaction as tx:
        snapshot: AppConfig = tx.view()
        assert type(snapshot) is dict
        snapshot["3xui"]["local"]["inbounds_list"].append(2)
        assert tx.view() is snapshot
        copied: JsonDict = tx.copy()
        assert copied == snapshot
        assert copied is not snapshot
        with pytest.raises(RuntimeError, match="transaction"):
            app_cfg.view()
    with pytest.raises(RuntimeError, match="not active"):
        transaction.view()
    assert app_cfg.view()["3xui"]["local"]["inbounds_list"] == [1, 2]
    with app_cfg as context_tx:
        context_document: AppConfig = context_tx.view()
        assert context_document["domain"] == "example.test"
    with app_cfg.edit() as edit_tx:
        edit_document: AppConfig = edit_tx.view()
        assert edit_document["domain"] == "example.test"


def test_transaction_view_rolls_back_nested_edits(app_cfg: Config[AppConfig]) -> None:
    transaction = app_cfg.edit()

    def abort_transaction() -> None:
        with transaction as tx:
            tx.view()["3xui"]["local"]["inbounds_list"].append(2)
            raise ValueError("abort")

    with pytest.raises(ValueError, match="abort"):
        abort_transaction()
    assert app_cfg.view()["3xui"]["local"]["inbounds_list"] == [1]
    with pytest.raises(RuntimeError, match="not active"):
        transaction.view()


@pytest.mark.parametrize("isolate", [True, False])
def test_leaked_transaction_view_obeys_commit_isolation(tmp_path: Path, isolate: bool) -> None:
    config = Config(path=tmp_path / "config.json", sync_mode="none", isolate_commits=isolate)
    try:
        with config.edit() as tx:
            leaked: JsonDict = tx.view()
            nested: dict[str, JsonValue] = {"value": 1}
            leaked["nested"] = nested
        nested["value"] = 2
        leaked["extra"] = True
        assert config.view() == ({"nested": {"value": 1}} if isolate else {
            "nested": {"value": 2}, "extra": True,
        })
        assert json.loads(Path(config.path).read_text(encoding="utf-8")) == {
            "nested": {"value": 1},
        }
    finally:
        config.close()


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


def test_panel_headers_reject_non_string_values(app_cfg: Config[AppConfig]) -> None:
    document = app_cfg.copy()
    panels = cast(JsonDict, document["3xui"])
    panel = cast(JsonDict, panels["local"])
    panel["inject_headers"] = {"X-Test": 42}
    with pytest.raises(SchemaValidationError):
        app_cfg.validate_document(document)


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
