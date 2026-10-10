from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, cast

import jsonschema

from .atomic import stat_signature
from .constants import JsonValue
from .jsonc import strip_jsonc_comments, strip_jsonc_trailing_commas

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .core import Config

from errors import ConfigError, FileCorruptionError, SchemaValidationError


def read_json_object[Doc](cfg: Config[Doc], /) -> dict[str, JsonValue]:
    try:
        with open(cfg.path, encoding="utf-8") as handle:
            content = handle.read()
        if cfg.read_only_jsonc:
            content = strip_jsonc_comments(content)
            content = strip_jsonc_trailing_commas(content)
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

def load_schema[Doc](cfg: Config[Doc], data: Mapping[str, JsonValue]) -> Mapping[str, JsonValue] | None:
    schema_ref = data.get("$schema")
    forced = cfg.schema_path
    if forced is not None:
        if schema_ref:
            cfg.log.warning(
                f"$schema {schema_ref!r} ignored; using schema_path {forced!r}"
            )
        schema_path = forced
    elif not schema_ref:
        return None
    else:
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
    schema_sig = stat_signature(schema_path)

    if (
        schema_path == cfg.schema_cache_path
        and schema_sig == cfg.schema_cache_signature
    ):
        return cfg.schema_cache

    try:
        with open(schema_path, encoding="utf-8") as handle:
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

    cfg.schema_cache_path = schema_path
    cfg.schema_cache_signature = schema_sig
    cfg.schema_cache = schema
    return schema

def validate_schema[Doc](cfg: Config[Doc], data: dict[str, JsonValue]) -> None:
    schema = load_schema(cfg, data)
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
