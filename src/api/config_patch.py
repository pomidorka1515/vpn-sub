from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal, cast

from config.equality import json_equal
from config.validation import check_config_document
from errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from config import JsonValue

# pyright: reportUnnecessaryIsInstance=false

# Captured once at process start. A change is reported; this module does not restart.
RESTART_KEYS: tuple[str, ...] = (
    "uri",
    "api_uri",
    "api_token",
    "3xui",
    "redis",
    "bot",
    "publicbot",
    "discord",
    "panel_alert_cooldown",
    "salt",
)

# Mirrors config.schema.json "required". The schema file is the source; the test
# fails if that array changes and this tuple is not updated.
REQUIRED_KEYS: tuple[str, ...] = (
    "uri",
    "api_token",
    "api_admin_ui_auth",
    "provider_id",
    "panel_alert_cooldown",
    "salt",
    "fingerprints",
    "nodes",
    "json_template",
    "bypass_packages",
    "domain",
    "ping_check_url",
    "3xui",
    "sub_name",
    "profiles",
    "redis",
)

SCHEMA_PROPERTIES: frozenset[str] = frozenset(
    (
        "$schema",
        *REQUIRED_KEYS,
        "bot",
        "publicbot",
        "discord",
        "support_text",
        "api_uri",
        "fallback_domain",
        "funny_strings",
    ),
)

type PatchStatus = Literal["updated", "unchanged"]


def config_etag(data: Mapping[str, JsonValue]) -> str:
    """Content revision. Whitespace is not part of the hash."""
    encoded = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def apply_config_patch(
    working: MutableMapping[str, JsonValue],
    values: Mapping[str, object],
) -> tuple[PatchStatus, list[str], list[str]]:
    """Replace listed top-level keys. null deletes an optional key. No deep merge.

    Returns ("unchanged", [], []) when the working copy equals its pre-image, so
    the caller skips backup and audit. Otherwise the changed key names (patch
    order) and the changed restart keys (table order).
    """
    if not isinstance(values, Mapping):
        raise ValidationError("values must be an object")

    required = set(REQUIRED_KEYS)
    for key in values:
        if key == "$schema":
            raise ValidationError("$schema is not patchable")
        if key not in SCHEMA_PROPERTIES:
            raise ValidationError(f"Unknown key: {key}")
        if values[key] is None and key in required:
            raise ValidationError(f"Cannot delete required key: {key}")

    before = {key: working.get(key) for key in RESTART_KEYS}
    preimage = dict(working)
    for key, value in values.items():
        if value is None:
            working.pop(key, None)
            continue
        if not _is_json_value(value):
            raise ValidationError(f"{key} is not a JSON value")
        working[key] = cast("JsonValue", value)
    check_config_document(working)
    if json_equal(dict(working), preimage):
        return "unchanged", [], []

    changed = [
        key
        for key in values
        if key not in preimage or not json_equal(working.get(key), preimage[key])
    ]
    restart = [key for key in RESTART_KEYS if not json_equal(working.get(key), before[key])]
    return "updated", changed, restart


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, int, bool)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and _is_json_value(item)
            for key, item in cast("Mapping[str, object]", value).items()
        )
    if isinstance(value, list):
        return all(_is_json_value(item) for item in cast("list[object]", value))
    return False
