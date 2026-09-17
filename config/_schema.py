import jsonschema
import json
import os

from collections.abc import Mapping
from typing import cast, TYPE_CHECKING
from ._constants import JsonValue
from ._atomic import _stat_signature
from ._jsonc import _strip_jsonc_comments, _strip_jsonc_trailing_commas

if TYPE_CHECKING:
    from ._core import Config

from errors import SchemaValidationError, ConfigError, FileCorruptionError

def _read_json_object(cfg: Config, /) -> dict[str, JsonValue]:
    try:
        with open(cfg.path, "r", encoding="utf-8") as handle:
            content = handle.read()
        if cfg.read_only_jsonc:
            content = _strip_jsonc_comments(content)
            content = _strip_jsonc_trailing_commas(content)
        data = json.loads(content)
    except FileNotFoundError:
        raise
    except json.JSONDecodeError as exc:
        raise FileCorruptionError(
            f"Config file '{cfg.path}' is not valid JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise ConfigError(
            f"Config file '{cfg.path}' must contain a JSON object at the top level."
        )
    return cast(dict[str, JsonValue], data)

def _load_schema(cfg: Config, data: Mapping[str, JsonValue]) -> Mapping[str, JsonValue] | None:
    schema_ref = data.get("$schema")
    if not schema_ref:
        return None

    if not isinstance(schema_ref, str):
        raise SchemaValidationError("'$schema' must be a string.")

    if schema_ref.startswith(("http://", "https://")):
        message = (
            "Remote JSON schemas are not supported for security/reliability reasons."
        )
        if cfg.strict_schema:
            raise SchemaValidationError(message)
        cfg.log.warning(message)

        return None

    schema_path = os.path.normpath(
        os.path.join(os.path.dirname(cfg.path), schema_ref)
    )
    schema_sig = _stat_signature(schema_path)

    if (
        schema_path == cfg._schema_cache_path
        and schema_sig == cfg._schema_cache_signature
    ):
        return cfg._schema_cache

    try:
        with open(schema_path, "r", encoding="utf-8") as handle:
            schema: dict[str, JsonValue] = json.load(handle)
    except FileNotFoundError as exc:
        if cfg.strict_schema:
            raise SchemaValidationError(
                f"Schema file not found: {schema_path}"
            ) from exc
        cfg.log.warning(f"Schema file not found: {schema_path}")
        return None
    except json.JSONDecodeError as exc:
        raise SchemaValidationError(
            f"Schema file '{schema_path}' is not valid JSON: {exc}"
        ) from exc

    if not isinstance(schema, dict): # pyright: ignore[reportUnnecessaryIsInstance]
        raise SchemaValidationError(
            f"Schema file '{schema_path}' must contain a JSON object."
        )

    cfg._schema_cache_path = schema_path
    cfg._schema_cache_signature = schema_sig
    cfg._schema_cache = schema
    return schema

def _validate_schema(cfg: Config, data: dict[str, JsonValue]) -> None:
    schema = _load_schema(cfg, data)
    if schema is None:
        return

    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as exc:
        message = (
            f"Schema validation error: {exc.message} "
            f"at path {list(exc.absolute_path)}"
        )
        if cfg.strict_schema:
            raise SchemaValidationError(message) from exc
        cfg.log.warning(message)
