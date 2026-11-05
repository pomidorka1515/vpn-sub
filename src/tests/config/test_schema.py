from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from config import Config
from errors import SchemaValidationError

if TYPE_CHECKING:
    from pathlib import Path

    from config import JsonDict


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
        json.dumps(
            {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
                "additionalProperties": True,
            },
        ),
        encoding="utf-8",
    )
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"$schema": "schema.json"}), encoding="utf-8")
    with pytest.raises(SchemaValidationError, match="Schema validation"):
        Config(path=path, backup_dir=None, sync_mode="none")


def test_local_schema_accepts_valid_document(tmp_path: Path) -> None:
    (tmp_path / "schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
                "additionalProperties": True,
            },
        ),
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


def test_schema_compilation_reuses_content_without_retaining_mutable_input() -> None:
    import fastjsonschema

    from config.schema import compile_schema

    schema: JsonDict = {"properties": {"cached_value": {"type": "string"}}}
    validator = compile_schema(schema)
    assert compile_schema(json.loads(json.dumps(schema))) is validator
    assert validator({"cached_value": "ok"}) == {"cached_value": "ok"}
    with pytest.raises(fastjsonschema.JsonSchemaValueException):
        validator({"cached_value": 1})

    schema["required"] = ["cached_value"]
    changed = compile_schema(schema)
    assert changed is not validator
    with pytest.raises(fastjsonschema.JsonSchemaValueException):
        changed({})
    assert validator({}) == {}


def test_schema_does_not_insert_defaults_and_recompiles_changes(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    schema: JsonDict = {"properties": {"optional": {"type": "string", "default": "default"}}}
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    config = Config(
        path=tmp_path / "config.json",
        schema_path=schema_path,
        sync_mode="none",
    )
    try:
        assert config.copy() == {}
        config["other"] = "ok"
        assert config.copy() == {"other": "ok"}
        schema["required"] = ["optional"]
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        with pytest.raises(SchemaValidationError, match="optional"):
            config.validate_document({"other": "ok"})
    finally:
        config.close()


def test_non_strict_schema_logs_validation_failure(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    schema_path.write_text('{"required": ["name"]}', encoding="utf-8")
    config = Config(
        path=tmp_path / "config.json",
        schema_path=schema_path,
        strict_schema=False,
        sync_mode="none",
    )
    try:
        config["other"] = "allowed"
        assert config.copy() == {"other": "allowed"}
    finally:
        config.close()


@pytest.mark.parametrize("forced", [False, True])
@pytest.mark.parametrize("scheme", ["http", "https"])
def test_strict_schema_rejects_remote_reference(
    tmp_path: Path,
    forced: bool,
    scheme: str,
) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"$schema": f"{scheme}://example.test/schema.json"}))
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    with pytest.raises(SchemaValidationError, match="Remote JSON schemas"):
        Config(path=path, strict_schema=True, schema_path=schema if forced else None)
