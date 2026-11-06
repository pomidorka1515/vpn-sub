from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, cast

import fastjsonschema

from .atomic import stat_signature
from .constants import JsonValue
from .jsonc import strip_jsonc_comments, strip_jsonc_trailing_commas

if TYPE_CHECKING:
    from collections.abc import Callable

    from .core import Config

from errors import ConfigError, FileCorruptionError, SchemaValidationError


def reject_nonfinite_constant(value: str) -> object:
    raise ValueError(f"Non-finite JSON number: {value}")


def validate_finite_numbers(value: object) -> None:
    """Reject non-finite numbers even in schema-free or extension data."""
    if isinstance(value, float) and not math.isfinite(value):
        raise SchemaValidationError("Config contains a non-finite number.")
    if isinstance(value, Mapping):
        for key, item in cast(Mapping[object, object], value).items():
            validate_finite_numbers(key)
            validate_finite_numbers(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in cast(Sequence[object], value):
            validate_finite_numbers(item)


def compile_schema(schema: Mapping[str, JsonValue]) -> Callable[[object], object]:
    """Compile a local Draft 7 schema without inserting defaults into config data."""
    # Config instances often use the same bundled schema. Key by content rather
    # than path/signature so edits invalidate the cache, even across instances.
    return _compile_schema(json.dumps(dict(schema)))


@lru_cache(maxsize=32)
def _compile_schema(document: str) -> Callable[[object], object]:
    return fastjsonschema.compile(json.loads(document), use_default=False, use_formats=False)


def read_json_object[Doc](cfg: Config[Doc], /) -> dict[str, JsonValue]:
    try:
        content = Path(cfg.path).read_text(encoding="utf-8")
        if cfg.read_only_jsonc:
            content = strip_jsonc_comments(content)
            content = strip_jsonc_trailing_commas(content)
        data = json.loads(content, parse_constant=reject_nonfinite_constant)
    except FileNotFoundError:
        raise
    except ValueError as exc:
        raise FileCorruptionError(f"Config file '{cfg.path}' is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"Config file '{cfg.path}' must contain a JSON object at the top level.")
    return cast(dict[str, JsonValue], data)


def load_schema[Doc](
    cfg: Config[Doc],
    data: Mapping[str, JsonValue],
) -> Mapping[str, JsonValue] | None:
    schema_ref = data.get("$schema")
    if isinstance(schema_ref, str) and schema_ref.startswith(("http://", "https://")):
        message = "Remote JSON schemas are not supported for security/reliability reasons."
        if cfg.strict_schema:
            raise SchemaValidationError(message)
        cfg.log.warning(message)
        if cfg.schema_path is None:
            return None

    forced = cfg.schema_path
    if forced is not None:
        if schema_ref:
            cfg.log.warning(f"$schema {schema_ref!r} ignored; using schema_path {forced!r}")
        schema_path = forced
    elif not schema_ref:
        return None
    else:
        if not isinstance(schema_ref, str):
            raise SchemaValidationError("'$schema' must be a string.")

        schema_path = os.path.normpath(Path(cfg.path).parent / schema_ref)
    schema_sig = stat_signature(schema_path)

    if schema_path == cfg.schema_cache_path and schema_sig == cfg.schema_cache_signature:
        return cfg.schema_cache

    try:
        with Path(schema_path).open(encoding="utf-8") as handle:
            schema: dict[str, JsonValue] = json.load(handle)
    except FileNotFoundError as exc:
        if cfg.strict_schema:
            raise SchemaValidationError(f"Schema file not found: {schema_path}") from exc
        cfg.log.warning(f"Schema file not found: {schema_path}")
        return None
    except json.JSONDecodeError as exc:
        raise SchemaValidationError(
            f"Schema file '{schema_path}' is not valid JSON: {exc}",
        ) from exc

    if not isinstance(schema, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise SchemaValidationError(f"Schema file '{schema_path}' must contain a JSON object.")

    validator = compile_schema(schema)
    cfg.schema_cache_path = schema_path
    cfg.schema_cache_signature = schema_sig
    cfg.schema_cache = schema
    cfg.schema_validator = validator
    return schema


def validate_schema[Doc](cfg: Config[Doc], data: dict[str, JsonValue]) -> None:
    validate_finite_numbers(data)
    schema = load_schema(cfg, data)
    if schema is None:
        return

    try:
        if cfg.schema_validator is not None:
            cfg.schema_validator(data)
    except fastjsonschema.JsonSchemaValueException as exc:
        message = f"Schema validation error: {exc.message} at path {exc.path[1:]}"
        if cfg.strict_schema:
            raise SchemaValidationError(message) from exc
        cfg.log.warning(message)
