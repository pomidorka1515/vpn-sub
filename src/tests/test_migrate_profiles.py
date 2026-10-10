from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from config import JsonValue

if TYPE_CHECKING:
    from collections.abc import Callable

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "migrate_profiles.py"
_SPEC = importlib.util.spec_from_file_location("migrate_profiles", _SCRIPT_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
mp = importlib.util.module_from_spec(_SPEC)
sys.modules["migrate_profiles"] = mp
_SPEC.loader.exec_module(mp)


def _legacy(**overrides: JsonValue) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {
        "$schema": "../config.schema.json",
        "uri": "sub",
        "api_token": "token",
        "api_admin_ui_auth": ["user", "pass"],
        "provider_id": "provider",
        "panel_alert_cooldown": 3600,
        "salt": "salt",
        "fingerprints": ["chrome"],
        "nodes": {"n1": "n1.example", "n2": "n2.example"},
        "bot": {"token": "admin", "whitelist": []},
        "json_template": {
            "dns": {},
            "routing": {"rules": [], "domainStrategy": "AsIs"},
            "inbounds": [],
            "outbounds": [],
            "remarks": "",
        },
        "bypass_packages": [],
        "domain": "example.test",
        "ping_check_url": "https://example.test/204",
        "publicbot": {"token": "public"},
        "3xui": {},
        "sub_name": "VPN",
        "flags": {"profile1": "🇫🇮", "fast": ""},
        "profiles": {
            "profile1": ["English", "Russian"],
            "fast": ["Fast", "Быстрый"],
        },
        "json_profiles": {
            "profile1": {"protocol": "vless"},
            "fast": {"protocol": "vless", "tag": "fast"},
        },
        "profileDescriptions": {
            "profile1": ["English", "Russian"],
            "fast": ["fast en", "fast ru"],
        },
        "whitelistProfiles": ["fast"],
        "xhttpExtra": {"profile1": {"path": "/x"}},
        "masterLinks": {
            "profile1": "vless://profile1",
            "fast": "vless://fast",
        },
        "profileNodes": {"profile1": "n1", "fast": "n2"},
        "shortProfileDescriptions": {
            "profile1": ["short en", "short ru"],
            "fast": ["fast short en", "fast short ru"],
        },
        "redis": {"url": "redis://127.0.0.1:6379/0"},
        "funny_strings": ["kept"],
    }
    data.update(overrides)
    return data


def _write(tmp_path: Path, data: dict[str, JsonValue], name: str = "config.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _read(path: Path) -> dict[str, JsonValue]:
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, JsonValue], loaded)


def _object(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def test_apply_joins_maps_and_preserves_key_order(tmp_path: Path) -> None:
    path = _write(tmp_path, _legacy())
    original = path.read_text(encoding="utf-8")

    assert mp.main(["--config", str(path), "--apply"]) == 0

    written = _read(path)
    assert list(written) == [
        "$schema", "uri", "api_token", "api_admin_ui_auth", "provider_id",
        "panel_alert_cooldown", "salt", "fingerprints", "nodes", "bot",
        "json_template", "bypass_packages", "domain", "ping_check_url",
        "publicbot", "3xui", "sub_name", "profiles", "redis", "funny_strings",
    ]
    profiles = _object(written["profiles"])
    assert list(profiles) == ["profile1", "fast"]
    profile1 = _object(profiles["profile1"])
    assert list(profile1) == [
        "flag", "name", "json", "description", "whitelist",
        "xhttpExtra", "masterLink", "node", "shortProfileDescription",
    ]
    assert profile1["flag"] == "🇫🇮"
    assert profile1["name"] == ["English", "Russian"]
    assert profile1["json"] == {"protocol": "vless"}
    assert profile1["whitelist"] is False
    assert profile1["xhttpExtra"] == {"path": "/x"}
    assert profile1["masterLink"] == "vless://profile1"
    assert profile1["node"] == "n1"
    fast = _object(profiles["fast"])
    assert fast["whitelist"] is True
    assert fast["xhttpExtra"] == {}
    assert fast["flag"] == ""
    assert written["funny_strings"] == ["kept"]
    assert written["$schema"] == "../config.schema.json"
    for key in mp.LEGACY_KEYS:
        assert key not in written

    backup = path.with_name("config.json.pre-v6")
    assert backup.read_text(encoding="utf-8") == original
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_missing_xhttp_extra_becomes_empty_object(tmp_path: Path) -> None:
    path = _write(tmp_path, _legacy(xhttpExtra={}))

    assert mp.main(["--config", str(path), "--apply"]) == 0

    written = _read(path)
    profiles = _object(written["profiles"])
    assert _object(profiles["profile1"])["xhttpExtra"] == {}
    assert _object(profiles["fast"])["xhttpExtra"] == {}


def test_dry_run_does_not_write(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, _legacy())
    before = path.read_bytes()

    assert mp.main(["--config", str(path)]) == 0

    assert path.read_bytes() == before
    assert not path.with_name("config.json.pre-v6").exists()
    out = capsys.readouterr().out
    assert "profile1, fast" in out
    assert "whitelist: fast" in out
    assert "xhttpExtra filled with {}: fast" in out
    assert "would delete:" in out
    assert "dry run" in out


def test_already_migrated_does_not_write(tmp_path: Path) -> None:
    source = _write(tmp_path, _legacy())
    assert mp.main(["--config", str(source), "--apply"]) == 0
    migrated = source.read_bytes()
    backup = source.with_name("config.json.pre-v6")
    backup_bytes = backup.read_bytes()

    assert mp.main(["--config", str(source), "--apply"]) == 0

    assert source.read_bytes() == migrated
    assert backup.read_bytes() == backup_bytes


def test_existing_pre_v6_backup_is_not_overwritten(tmp_path: Path) -> None:
    path = _write(tmp_path, _legacy())
    backup = path.with_name("config.json.pre-v6")
    backup.write_text("original backup\n", encoding="utf-8")

    assert mp.main(["--config", str(path), "--apply"]) == 0

    assert backup.read_text(encoding="utf-8") == "original backup\n"
    assert "profiles" in _read(path)
    assert "flags" not in _read(path)


def _drop_flag(data: dict[str, JsonValue]) -> None:
    _object(data["flags"]).pop("fast")


def _add_orphan_profile(data: dict[str, JsonValue]) -> None:
    _object(data["json_profiles"])["orphan"] = {}


def _add_orphan_whitelist(data: dict[str, JsonValue]) -> None:
    cast(list[str], data["whitelistProfiles"]).append("nope")


def _add_orphan_xhttp(data: dict[str, JsonValue]) -> None:
    _object(data["xhttpExtra"])["nope"] = {}


def _point_at_missing_node(data: dict[str, JsonValue]) -> None:
    _object(data["profileNodes"])["fast"] = "missing"


def _make_json_not_object(data: dict[str, JsonValue]) -> None:
    _object(data["json_profiles"])["fast"] = "not-object"


def _mix_profile_shapes(data: dict[str, JsonValue]) -> None:
    _object(data["profiles"])["fast"] = {"flag": ""}


def _drop_profiles(data: dict[str, JsonValue]) -> None:
    data.pop("profiles")


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (_drop_flag, "flags is missing"),
        (_add_orphan_profile, "not in profiles"),
        (_add_orphan_whitelist, "whitelistProfiles"),
        (_add_orphan_xhttp, "xhttpExtra"),
        (_point_at_missing_node, "not a key of nodes"),
        (_make_json_not_object, "must be an object"),
        (_mix_profile_shapes, "mixed config"),
        (_drop_profiles, "profiles must be an object"),
    ],
)
def test_refuses_without_writing(
    tmp_path: Path,
    mutate: Callable[[dict[str, JsonValue]], object],
    match: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data = _legacy()
    mutate(data)
    path = _write(tmp_path, data)
    before = path.read_bytes()

    assert mp.main(["--config", str(path), "--apply"]) == 1

    assert path.read_bytes() == before
    assert not path.with_name("config.json.pre-v6").exists()
    assert match in capsys.readouterr().err


def test_schema_validation_failure_does_not_replace_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write(tmp_path, _legacy())
    before = path.read_bytes()

    def fail(data: object, schema: object) -> None:
        del data, schema
        raise mp.jsonschema.ValidationError("forced failure")

    monkeypatch.setattr(mp.jsonschema, "validate", fail)

    assert mp.main(["--config", str(path), "--apply"]) == 1

    assert path.read_bytes() == before
    assert not path.with_name("config.json.pre-v6").exists()
    assert "schema validation" in capsys.readouterr().err


def test_missing_file_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "missing.json"

    assert mp.main(["--config", str(missing)]) == 1

    assert "not found" in capsys.readouterr().err
