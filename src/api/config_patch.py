from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, MutableMapping
from typing import Literal, cast

from config import JsonValue
from errors import ValidationError

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

SCHEMA_PROPERTIES: frozenset[str] = frozenset((
    "$schema",
    *REQUIRED_KEYS,
    "bot",
    "publicbot",
    "api_uri",
    "fallback_domain",
    "funny_strings",
))

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


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and value != ""


def check_config_document(data: Mapping[str, object]) -> None:
    """Refuse a document this process cannot boot, and a profile node that is not a node.

    A wrong type falls through so schema validation raises SchemaValidationError
    instead of TypeError. Does not tighten config.schema.json.
    """
    profiles = data.get("profiles")
    nodes = data.get("nodes")
    if isinstance(profiles, Mapping) and isinstance(nodes, Mapping):
        profiles, nodes = cast(Mapping[str, object], profiles), cast(Mapping[str, object], nodes)
        for profile_id, profile in profiles.items():
            if not isinstance(profile, Mapping):
                continue
            profile = cast(Mapping[str, object], profile)
            node = profile.get("node")
            if isinstance(node, str) and node not in nodes:
                raise ValidationError(
                    f"profiles.{profile_id}.node {node!r} is not a key of nodes"
                )

    panels = data.get("3xui")
    if isinstance(panels, Mapping) and len(cast(Mapping[str, object], panels)) == 0:
        raise ValidationError("3xui must contain at least one panel")

    publicbot = data.get("publicbot")
    if isinstance(publicbot, Mapping):
        token = cast(Mapping[str, object], publicbot).get("token")
        if not isinstance(token, str):
            raise ValidationError("publicbot.token must be a string")

    bot = data.get("bot")
    if isinstance(bot, Mapping):
        token = cast(Mapping[str, object], bot).get("token")
        if not isinstance(token, str):
            raise ValidationError("bot.token must be a string")

    if not _nonempty_str(data.get("api_token")):
        raise ValidationError("api_token must not be empty")

    auth = data.get("api_admin_ui_auth")
    if (
        isinstance(auth, list)
        and len(cast(list[object], auth)) == 2
        and (
            not _nonempty_str(cast(list[object], auth)[0]) or
            not _nonempty_str(cast(list[object], auth)[1])
        )
    ):
        raise ValidationError("api_admin_ui_auth entries must not be empty")


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
        working[key] = cast(JsonValue, value)
    check_config_document(working)
    if _json_equal(dict(working), preimage):
        return "unchanged", [], []

    changed = [
        key for key in values
        if key not in preimage or not _json_equal(working.get(key), preimage[key])
    ]
    restart = [
        key for key in RESTART_KEYS
        if not _json_equal(working.get(key), before[key])
    ]
    return "updated", changed, restart


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, int, bool)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in cast(Mapping[str, object], value).items())
    if isinstance(value, list):
        return all(_is_json_value(item) for item in cast(list[object], value))
    return False


def _json_equal(left: object, right: object) -> bool:
    """Value equality that does not treat 1 and 1.0 as the same config value."""
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        left, right = cast(Mapping[object, object], left), cast(Mapping[object, object], right)
        if left.keys() != right.keys():
            return False
        return all(_json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        left, right = cast(list[object], left), cast(list[object], right)
        if len(left) != len(right):
            return False
        return all(_json_equal(item, other) for item, other in zip(left, right, strict=True))
    return left == right
