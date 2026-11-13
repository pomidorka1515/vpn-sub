from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from config import Config
from config.atomic import atomic_write_json
from errors import ConfigError, FileCorruptionError, SchemaValidationError

if TYPE_CHECKING:
    from config import AppConfig


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
@pytest.mark.parametrize("jsonc", [False, True])
def test_load_rejects_nonfinite_numbers(tmp_path: Path, literal: str, jsonc: bool) -> None:
    path = tmp_path / ("config.jsonc" if jsonc else "config.json")
    content = f'{{"extension": [{{"value": {literal}}}]}}'
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        Config(path=path, read_only=jsonc, sync_mode="none")
    assert path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
@pytest.mark.parametrize("operation", ["reload", "read", "transaction"])
def test_failed_reload_preserves_state_and_external_file(
    tmp_path: Path, literal: str, operation: str
) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"valid": 1}', encoding="utf-8")
    cfg = Config(path=path, sync_mode="none")
    try:
        signature = cfg.last_signature
        content = f'{{"extension": [{{"value": {literal}}}]}}'
        path.write_text(content, encoding="utf-8")

        def refresh() -> None:
            if operation == "reload":
                cfg.reload()
            elif operation == "read":
                cfg.view()
            else:
                with cfg.edit():
                    pass

        with pytest.raises(ConfigError):
            refresh()
        assert cfg.data == {"valid": 1}
        assert cfg.last_signature == signature
        assert path.read_text(encoding="utf-8") == content
        path.write_text('{"valid": 2}', encoding="utf-8")
        assert cfg.reload()
        assert cfg.view() == {"valid": 2}
    finally:
        cfg.close()


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("with_schema", [False, True])
def test_programmatic_validation_and_commit_reject_nested_nonfinite(
    tmp_path: Path, number: float, strict: bool, with_schema: bool
) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text('{"type": "object", "additionalProperties": true}', encoding="utf-8")
    path = tmp_path / "config.json"
    cfg = Config(
        path=path,
        schema_path=schema if with_schema else None,
        strict_schema=strict,
        sync_mode="none",
    )
    try:
        cfg["valid"] = 1
        original = path.read_bytes()
        signature = cfg.last_signature
        invalid = {"extension": [{"nested": (0, number)}]}
        with pytest.raises(SchemaValidationError, match="non-finite"):
            cfg.validate_document(invalid)
        with pytest.raises(SchemaValidationError, match="non-finite"), cfg.edit() as tx:
            tx.update(invalid)
        assert cfg.view() == {"valid": 1}
        assert cfg.last_signature == signature
        assert path.read_bytes() == original
    finally:
        cfg.close()


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -float("inf")])
def test_app_socket_timeout_rejects_nonfinite(app_cfg: Config[AppConfig], number: float) -> None:
    path = app_cfg.path
    before = Path(path).read_bytes()
    previous = app_cfg.view()
    with pytest.raises(SchemaValidationError, match="non-finite"), app_cfg.edit() as tx:
        tx.view()["redis"]["socket_timeout"] = number
    assert app_cfg.view() == previous
    assert Path(path).read_bytes() == before


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("minify", [False, True])
def test_atomic_write_refuses_nonfinite_and_cleans_temp_file(
    tmp_path: Path, number: float, minify: bool
) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"valid": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="Out of range float"):
        atomic_write_json(
            str(path), {"nested": [number]}, minify=minify, indent=4, sync_mode="none"
        )
    assert path.read_text(encoding="utf-8") == '{"valid": 1}'
    assert list(tmp_path.glob(".tmp_*.json")) == []


def test_nonfinite_parse_constant_reports_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"value": NaN}', encoding="utf-8")
    with pytest.raises(FileCorruptionError, match="Non-finite JSON number"):
        Config(path=path, sync_mode="none")
