"""One-shot migration: fold scattered profile maps into one object per inbound.

v6 replaces the top-level ``flags``, ``json_profiles``, ``profileDescriptions``,
``whitelistProfiles``, ``xhttpExtra``, ``masterLinks``, ``profileNodes``, and
``shortProfileDescriptions`` keys with a single ``profiles`` object. This script
is the ONLY place the old layout is understood; the service has no compat path.

Stop the service before ``--apply``. A running v5 process reloads on file
change and will misread the new objects. A running v6 process will not start
until this has been applied. ``vpn-sub --update`` replaces the binary and does
not migrate ``config.json``.

Usage::

    venv/bin/python scripts/migrate_profiles.py            # dry-run (default)
    venv/bin/python scripts/migrate_profiles.py --apply    # rewrite config.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import jsonschema

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonDict = dict[str, JsonValue]

_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA_PATH = _ROOT / "config.schema.json"

LEGACY_KEYS = (
    "flags",
    "json_profiles",
    "profileDescriptions",
    "whitelistProfiles",
    "xhttpExtra",
    "masterLinks",
    "profileNodes",
    "shortProfileDescriptions",
)
REQUIRED_MAPS = (
    "flags",
    "json_profiles",
    "profileDescriptions",
    "masterLinks",
    "profileNodes",
    "shortProfileDescriptions",
)
BILINGUAL_SOURCES = ("profileDescriptions", "shortProfileDescriptions")
PROFILE_FIELDS = (
    "flag",
    "name",
    "json",
    "description",
    "whitelist",
    "xhttpExtra",
    "masterLink",
    "node",
    "shortProfileDescription",
)


class MigrateError(Exception):
    """The file cannot be migrated. Nothing is written."""


def _is_object(value: object) -> bool:
    return isinstance(value, dict)


def _is_bilingual(value: object) -> bool:
    if not isinstance(value, list):
        return False
    pair = cast(list[object], value)
    return len(pair) == 2 and all(isinstance(item, str) for item in pair)


def _load(path: Path) -> JsonDict:
    if not path.is_file():
        raise MigrateError(f"config not found: {path}")
    try:
        with path.open(encoding="utf-8") as handle:
            data: object = json.load(handle)
    except json.JSONDecodeError as exc:
        raise MigrateError(f"config is not valid JSON: {exc}") from exc
    if not _is_object(data):
        raise MigrateError("config must be a JSON object")
    return cast(JsonDict, data)


def _already_migrated(data: Mapping[str, JsonValue]) -> bool:
    if any(key in data for key in LEGACY_KEYS):
        return False
    profiles = data.get("profiles")
    if not _is_object(profiles):
        return False
    profile_map = cast(JsonDict, profiles)
    if not profile_map:
        return False
    return all(
        _is_object(profile)
        and all(field in cast(JsonDict, profile) for field in PROFILE_FIELDS)
        for profile in profile_map.values()
    )


def _require_object(data: Mapping[str, JsonValue], key: str) -> JsonDict:
    value = data.get(key)
    if not _is_object(value):
        raise MigrateError(f"{key} must be an object")
    return cast(JsonDict, value)


def _key_set(value: object, label: str) -> set[str]:
    if isinstance(value, dict):
        return set(cast(JsonDict, value))
    if isinstance(value, list):
        items = cast(list[object], value)
        if not all(isinstance(item, str) for item in items):
            raise MigrateError(f"{label} must contain only strings")
        return set(cast(list[str], items))
    raise MigrateError(f"{label} must be an object or array of ids")


def build_profiles(data: Mapping[str, JsonValue]) -> tuple[dict[str, JsonDict], list[str]]:
    """Join the legacy maps. Refuse rather than drop or invent ids."""
    profiles = data.get("profiles")
    if not _is_object(profiles):
        raise MigrateError("profiles must be an object")
    names = cast(JsonDict, profiles)
    if any(_is_object(value) for value in names.values()):
        raise MigrateError("mixed config: profiles values are already objects and legacy keys are still present")

    ids = list(names)
    id_set = set(ids)
    maps = {key: _require_object(data, key) for key in REQUIRED_MAPS}
    nodes = data.get("nodes")
    if not _is_object(nodes):
        raise MigrateError("nodes must be an object")
    node_ids = set(cast(JsonDict, nodes))

    for key, mapping in maps.items():
        present = set(mapping)
        missing = [profile_id for profile_id in ids if profile_id not in present]
        orphans = sorted(present - id_set)
        if missing:
            raise MigrateError(f"{key} is missing id(s): {', '.join(missing)}")
        if orphans:
            raise MigrateError(f"{key} has id(s) not in profiles: {', '.join(orphans)}")

    whitelist = data.get("whitelistProfiles", [])
    extra = data.get("xhttpExtra", {})
    whitelist_ids = _key_set(whitelist, "whitelistProfiles")
    extra_ids = _key_set(extra, "xhttpExtra")
    if not isinstance(whitelist, list):
        raise MigrateError("whitelistProfiles must be an array")
    if not _is_object(extra):
        raise MigrateError("xhttpExtra must be an object")
    extra_map = cast(JsonDict, extra)
    for label, present in (("whitelistProfiles", whitelist_ids), ("xhttpExtra", extra_ids)):
        orphans = sorted(present - id_set)
        if orphans:
            raise MigrateError(f"{label} has id(s) not in profiles: {', '.join(orphans)}")

    filled: list[str] = []
    built: dict[str, JsonDict] = {}
    for profile_id in ids:
        name = names[profile_id]
        if not _is_bilingual(name):
            raise MigrateError(f"profiles[{profile_id}] must be a length-2 array of strings")
        flag = maps["flags"][profile_id]
        master = maps["masterLinks"][profile_id]
        node = maps["profileNodes"][profile_id]
        outbound = maps["json_profiles"][profile_id]
        if not isinstance(flag, str):
            raise MigrateError(f"flags[{profile_id}] must be a string")
        if not isinstance(master, str):
            raise MigrateError(f"masterLinks[{profile_id}] must be a string")
        if not isinstance(node, str):
            raise MigrateError(f"profileNodes[{profile_id}] must be a string")
        if node not in node_ids:
            raise MigrateError(f"profileNodes[{profile_id}] is not a key of nodes")
        if not _is_object(outbound):
            raise MigrateError(f"json_profiles[{profile_id}] must be an object")
        for source in BILINGUAL_SOURCES:
            if not _is_bilingual(maps[source][profile_id]):
                raise MigrateError(f"{source}[{profile_id}] must be a length-2 array of strings")
        if profile_id in extra_map:
            xhttp = extra_map[profile_id]
            if not _is_object(xhttp):
                raise MigrateError(f"xhttpExtra[{profile_id}] must be an object")
        else:
            xhttp = {}
            filled.append(profile_id)
        built[profile_id] = {
            "flag": flag,
            "name": name,
            "json": outbound,
            "description": maps["profileDescriptions"][profile_id],
            "whitelist": profile_id in whitelist_ids,
            "xhttpExtra": xhttp,
            "masterLink": master,
            "node": node,
            "shortProfileDescription": maps["shortProfileDescriptions"][profile_id],
        }
    return built, filled


def rewrite(data: Mapping[str, JsonValue], profiles: Mapping[str, JsonDict]) -> JsonDict:
    """Keep top-level order. Emit the new map where ``profiles`` was; skip legacy keys."""
    rewritten: JsonDict = {}
    for key, value in data.items():
        if key in LEGACY_KEYS:
            continue
        rewritten[key] = dict(profiles) if key == "profiles" else value
    return rewritten


def _validate(data: Mapping[str, JsonValue]) -> None:
    try:
        with _SCHEMA_PATH.open(encoding="utf-8") as handle:
            loaded: object = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrateError(f"cannot load schema {_SCHEMA_PATH}: {exc}") from exc
    if not _is_object(loaded):
        raise MigrateError(f"schema {_SCHEMA_PATH} must be a JSON object")
    schema = cast(JsonDict, loaded)
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as exc:
        raise MigrateError(f"migrated config failed schema validation: {exc.message}") from exc


def _write(path: Path, data: Mapping[str, JsonValue]) -> None:
    directory = path.parent
    backup = path.with_name(path.name + ".pre-v6")
    # A second --apply on a still-legacy file may proceed. The first backup
    # is the original and must not be replaced.
    keep_backup = backup.exists()
    fd, temp_name = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".json")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=4, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if keep_backup:
            path.unlink()
        else:
            os.replace(path, backup)
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _report(
    profiles: Mapping[str, JsonDict],
    filled: list[str],
    *,
    apply: bool,
) -> None:
    verb = "" if apply else "would "
    print(f"inbounds: {', '.join(profiles) if profiles else '(none)'}")
    whitelist = [profile_id for profile_id, profile in profiles.items() if profile["whitelist"]]
    print(f"whitelist: {', '.join(whitelist) if whitelist else '(none)'}")
    print(f"xhttpExtra filled with {{}}: {', '.join(filled) if filled else '(none)'}")
    print(f"{verb}delete: {', '.join(LEGACY_KEYS)}")


def migrate(path: Path, *, apply: bool) -> int:
    data = _load(path)
    if _already_migrated(data):
        print(f"already migrated: {path}")
        return 0
    profiles, filled = build_profiles(data)
    rewritten = rewrite(data, profiles)
    _validate(rewritten)
    _report(profiles, filled, apply=apply)
    if not apply:
        print("dry run — no changes applied (pass --apply to write)")
        return 0
    _write(path, rewritten)
    print(f"wrote {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fold legacy profile maps into one object per inbound.",
    )
    parser.add_argument(
        "--config",
        default=os.getenv("PATH_CONFIG", str(_ROOT / "data" / "config.json")),
        help="service config path (default: data/config.json or PATH_CONFIG)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="rewrite the file (default: dry-run)",
    )
    args = parser.parse_args(argv)
    try:
        return migrate(Path(args.config), apply=args.apply)
    except MigrateError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
