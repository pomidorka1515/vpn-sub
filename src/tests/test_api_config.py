from __future__ import annotations

from typing import Any, cast
import json

import pytest
from flask import Flask
from pathlib import Path

from api import Api
from api.config_patch import REQUIRED_KEYS, config_etag
from config import Config, ConfigLike, JsonValue, LinesConfigLike
from db import Database
from helpers import make_subscription, make_watch

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "config.schema.json"
_EXAMPLE_PATH = Path(__file__).resolve().parents[2] / "docs" / "EXAMPLE.config.json"


@pytest.fixture
def flask_app() -> Flask:
    return Flask(__name__)


class _RecordingAudit:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def append(self, record: object) -> None:
        self.records.append(cast(dict[str, object], record))


def _valid_config() -> dict[str, Any]:
    data = json.loads(_EXAMPLE_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    data = cast(dict[str, object], data)
    data["$schema"] = "../config.schema.json"
    data["api_token"] = "secret-token"
    data["api_admin_ui_auth"] = ["admin", "panel-secret"]
    data["publicbot"] = {"token": "123:public"}
    data["sub_name"] = "before"
    data["funny_strings"] = ["keep-me"]
    return data


def _bad_node_profiles() -> dict[str, Any]:
    profile = dict(_valid_config()["profiles"]["profile1"])
    profile["node"] = "missing"
    return {"profile1": profile}


def _config_api(tmp_path: Path, flask_app: Flask) -> tuple[Config, Path, _RecordingAudit]:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_valid_config(), indent=4) + "\n", encoding="utf-8")
    cfg = Config(
        path=path,
        schema_path=_SCHEMA_PATH,
        backup_dir=tmp_path / "backup",
        sync_mode="none",
        start_backup=False,
    )
    audit = _RecordingAudit()
    database = Database(path=tmp_path / "state.sqlite3", timeout=2)
    subscription = make_subscription(
        database,
        app=flask_app,
        audit_cfg=cast(LinesConfigLike, audit),
    )
    Api(
        app=flask_app,
        cfg=cast(ConfigLike, cfg),
        audit_cfg=cast(LinesConfigLike, audit),
        sub=subscription,
        bw=make_watch(subscription.res.db, subscription),
    )
    database.close()
    return cfg, path, audit


def _schema_required() -> list[str]:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    required = schema["required"]
    assert isinstance(required, list)
    return [item for item in cast(list[object], required) if isinstance(item, str)]


def test_required_keys_match_schema() -> None:
    assert list(REQUIRED_KEYS) == _schema_required()


def test_config_get_requires_token(tmp_path: Path, flask_app: Flask) -> None:
    cfg, _path, _audit = _config_api(tmp_path, flask_app)
    try:
        denied = flask_app.test_client().get("/sub/api/config/get")
        assert denied.status_code == 401
        denied_set = flask_app.test_client().post("/sub/api/config/set", json={"base": "x", "values": {}})
        assert denied_set.status_code == 401
    finally:
        cfg.close()



def test_config_get_returns_file_and_stable_etag(tmp_path: Path, flask_app: Flask) -> None:
    cfg, path, _audit = _config_api(tmp_path, flask_app)
    try:
        client = flask_app.test_client()
        headers = {"Authorization": "secret-token"}
        first = client.get("/sub/api/config/get", headers=headers)
        second = client.get("/sub/api/config/get", headers=headers)
        assert first.status_code == 200
        payload = first.get_json()
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert payload["obj"] == on_disk
        body = first.data.decode("utf-8")
        assert body.count("\n") == 1 and body.endswith("\n")
        assert ": " not in body and ", " not in body
        assert first.headers["ETag"] == f'"{config_etag(cast(dict[str, JsonValue], on_disk))}"'
        assert first.headers["Cache-Control"] == "no-store"
        obj_at = body.index('"obj":')
        schema_at = body.index('"$schema":')
        assert obj_at < schema_at
        assert second.headers["ETag"] == first.headers["ETag"]
        assert payload["obj"]["api_token"] == "secret-token"
    finally:
        cfg.close()


def _set(
    flask_app: Flask,
    base: str,
    values: dict[str, Any],
) -> Any:
    return flask_app.test_client().post(
        "/sub/api/config/set",
        json={"base": base, "values": values},
        headers={"Authorization": "secret-token"},
    )


def test_config_set_one_key_keeps_indent_and_other_keys(tmp_path: Path, flask_app: Flask) -> None:
    cfg, path, audit = _config_api(tmp_path, flask_app)
    try:
        before = path.read_bytes()
        original = json.loads(before)
        base = config_etag(cast(dict[str, JsonValue], original))
        response = _set(flask_app, base, {"sub_name": "after"})
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["msg"] == "Updated"
        assert payload["obj"]["restart"] == []
        written = path.read_text(encoding="utf-8")
        assert written.endswith("\n")
        assert '\n    "sub_name": "after"' in written
        updated = json.loads(written)
        original["sub_name"] = "after"
        assert updated == original
        assert payload["obj"]["base"] == config_etag(cast(dict[str, JsonValue], updated))
        assert audit.records[-1]["action"] == "config_update"
        info = audit.records[-1]["info"]
        assert isinstance(info, dict)
        assert info["keys"] == ["sub_name"]
        assert "after" not in json.dumps(info)
    finally:
        cfg.close()


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"nope": 1}, "Unknown key"),
        ({"panel_alert_cooldown": "soon"}, "Schema validation"),
        ({"3xui": {}}, "3xui must contain"),
        ({"$schema": "other.json"}, "$schema"),
    ],
)
def test_config_set_rejects_without_writing(
    tmp_path: Path,
    flask_app: Flask,
    values: dict[str, Any],
    message: str,
) -> None:
    cfg, path, audit = _config_api(tmp_path, flask_app)
    try:
        before = path.read_bytes()
        base = config_etag(cast(dict[str, JsonValue], json.loads(before)))
        response = _set(flask_app, base, values)
        assert response.status_code == 400
        assert message in response.get_json()["msg"]
        assert path.read_bytes() == before
        assert audit.records == []
        assert not (tmp_path / "backup").exists()
    finally:
        cfg.close()


def test_config_set_rejects_unknown_profile_node(tmp_path: Path, flask_app: Flask) -> None:
    cfg, path, audit = _config_api(tmp_path, flask_app)
    try:
        before = path.read_bytes()
        base = config_etag(cast(dict[str, JsonValue], json.loads(before)))
        response = _set(flask_app, base, {"profiles": _bad_node_profiles()})
        assert response.status_code == 400
        assert "not a key of nodes" in response.get_json()["msg"]
        assert path.read_bytes() == before
        assert audit.records == []
    finally:
        cfg.close()


def test_config_set_stale_base_is_409(tmp_path: Path, flask_app: Flask) -> None:
    cfg, path, _audit = _config_api(tmp_path, flask_app)
    try:
        before = path.read_bytes()
        current = config_etag(cast(dict[str, JsonValue], json.loads(before)))
        response = _set(flask_app, "0" * 64, {"sub_name": "after"})
        assert response.status_code == 409
        payload = response.get_json()
        assert payload["msg"] == "Config changed since it was loaded"
        assert payload["obj"]["base"] == current
        assert path.read_bytes() == before
    finally:
        cfg.close()


def test_config_set_rejects_a_non_object_body(tmp_path: Path, flask_app: Flask) -> None:
    cfg, path, _audit = _config_api(tmp_path, flask_app)
    try:
        before = path.read_bytes()
        client = flask_app.test_client()
        headers = {"Authorization": "secret-token"}
        missing = client.post("/sub/api/config/set", json={"values": {}}, headers=headers)
        assert missing.status_code == 400
        listed = client.post("/sub/api/config/set", json=["nope"], headers=headers)
        assert listed.status_code == 400
        assert path.read_bytes() == before
        quoted = _set(
            flask_app,
            f'"{config_etag(cast(dict[str, JsonValue], json.loads(before)))}"',
            {"sub_name": "after"},
        )
        assert quoted.status_code == 200
        assert json.loads(path.read_text(encoding="utf-8"))["sub_name"] == "after"
    finally:
        cfg.close()


def test_config_set_null_deletes_optional_and_rejects_required(
    tmp_path: Path,
    flask_app: Flask,
) -> None:
    cfg, path, _audit = _config_api(tmp_path, flask_app)
    try:
        before = path.read_bytes()
        base = config_etag(cast(dict[str, JsonValue], json.loads(before)))
        rejected = _set(flask_app, base, {"domain": None})
        assert rejected.status_code == 400
        assert "required" in rejected.get_json()["msg"]
        assert path.read_bytes() == before

        removed = _set(flask_app, base, {"funny_strings": None, "domain": "https://example.test"})
        assert removed.status_code == 200
        updated = json.loads(path.read_text(encoding="utf-8"))
        assert "funny_strings" not in updated
        assert updated["domain"] == "https://example.test"
        assert updated["sub_name"] == "before"
    finally:
        cfg.close()


def test_config_set_unchanged_does_not_touch_mtime(tmp_path: Path, flask_app: Flask) -> None:
    cfg, path, audit = _config_api(tmp_path, flask_app)
    try:
        before = path.read_bytes()
        mtime = path.stat().st_mtime_ns
        base = config_etag(cast(dict[str, JsonValue], json.loads(before)))
        response = _set(flask_app, base, {"sub_name": "before"})
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["msg"] == "Unchanged"
        assert payload["obj"]["restart"] == []
        assert path.read_bytes() == before
        assert path.stat().st_mtime_ns == mtime
        assert audit.records == []
    finally:
        cfg.close()


def test_config_set_restart_lists_only_captured_keys(tmp_path: Path, flask_app: Flask) -> None:
    cfg, path, _audit = _config_api(tmp_path, flask_app)
    try:
        original = json.loads(path.read_text(encoding="utf-8"))
        base = config_etag(cast(dict[str, JsonValue], original))
        domain = _set(flask_app, base, {"domain": "https://other.test"})
        assert domain.status_code == 200
        assert domain.get_json()["obj"]["restart"] == []

        panels = json.loads(path.read_text(encoding="utf-8"))["3xui"]
        panels["local_panel"]["name"] = "renamed"
        changed = _set(flask_app, domain.get_json()["obj"]["base"], {"3xui": panels})
        assert changed.status_code == 200
        assert changed.get_json()["obj"]["restart"] == ["3xui"]
    finally:
        cfg.close()


def test_config_set_failed_commit_does_not_back_up_or_audit(
    tmp_path: Path,
    flask_app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, path, audit = _config_api(tmp_path, flask_app)
    try:
        before = path.read_bytes()
        base = config_etag(cast(dict[str, JsonValue], json.loads(before)))

        def fail_write(data: dict[str, JsonValue]) -> None:
            del data
            raise OSError("disk full")

        monkeypatch.setattr(cfg, "_atomic_write", fail_write)
        response = _set(flask_app, base, {"sub_name": "after"})
        assert response.status_code == 500
        assert path.read_bytes() == before
        assert audit.records == []
        assert not (tmp_path / "backup").exists()
    finally:
        cfg.close()
