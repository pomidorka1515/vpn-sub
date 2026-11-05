from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from config import AppConfig, Config, JsonDict
from config.validation import check_config_document
from errors import SchemaValidationError, ValidationError

if TYPE_CHECKING:
    from config import JsonValue


def test_whitelist_panel_limit_on_commit_and_reload(app_cfg: Config[AppConfig]) -> None:
    with app_cfg.edit() as tx:
        panels = tx.view()["3xui"]
        panels["local"]["whitelist"] = True
        panels["regular"] = {**panels["local"], "name": "regular", "whitelist": False}
    before = Path(app_cfg.path).read_bytes()
    with pytest.raises(SchemaValidationError, match="At most one whitelist panel"):
        with app_cfg.edit() as tx:
            tx.view()["3xui"]["regular"]["whitelist"] = True
    assert Path(app_cfg.path).read_bytes() == before
    document = app_cfg.view()
    document["3xui"]["regular"]["whitelist"] = True
    Path(app_cfg.path).write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SchemaValidationError, match="At most one whitelist panel"):
        app_cfg.reload()


@pytest.mark.parametrize("boundary", ["load", "reload", "commit", "validate"])
@pytest.mark.parametrize(
    "invalid",
    ["profile_node", "removed_node", "username", "password", "nginx_auth"],
)
def test_business_validation_at_config_boundaries(
    app_cfg: Config[AppConfig],
    boundary: str,
    invalid: str,
) -> None:
    with app_cfg.edit() as tx:
        tx["nodes"] = {"local": "example.test"}
        tx["profiles"] = {
            "profile": {
                "flag": "",
                "name": ["local", "local"],
                "json": {
                    "settings": {"vnext": [{"users": [{}]}]},
                    "streamSettings": {},
                },
                "description": ["", ""],
                "whitelist": False,
                "xhttpExtra": {},
                "masterLink": "",
                "node": "local",
                "shortProfileDescription": ["", ""],
            },
        }
    snapshot = app_cfg.copy()
    document = app_cfg.view()
    if invalid == "profile_node":
        document["profiles"]["profile"]["node"] = "missing"
    elif invalid == "removed_node":
        document["nodes"] = {}
    elif invalid == "nginx_auth":
        document["3xui"]["local"]["nginx_auth"] = ["proxy-user", "proxy-password"]
    else:
        document["api_admin_ui_auth"][0 if invalid == "username" else 1] = ""
    if invalid == "nginx_auth":
        message = "nginx_auth is incompatible with panel bearer authentication"
    else:
        message = "not a key of nodes" if "node" in invalid else "entries must not be empty"
    path = Path(app_cfg.path)
    before = path.read_bytes()

    def exercise_boundary() -> None:
        if boundary == "load":
            path.write_text(json.dumps(document), encoding="utf-8")
            Config[AppConfig](
                path=path,
                schema_path=app_cfg.schema_path,
                validators=(check_config_document,),
                backup_dir=None,
                sync_mode="none",
            )
        elif boundary == "reload":
            path.write_text(json.dumps(document), encoding="utf-8")
            app_cfg.reload()
        elif boundary == "commit":
            with app_cfg.edit() as tx:
                tx.update(cast("JsonDict", document))
        else:
            app_cfg.validate_document(cast("JsonDict", document))

    with pytest.raises(ValidationError, match=message):
        exercise_boundary()
    assert app_cfg.data == snapshot
    if boundary in {"commit", "validate"}:
        assert path.read_bytes() == before
    else:
        assert json.loads(path.read_bytes()) == document


@pytest.mark.parametrize("whitelist", [False, True])
def test_duplicate_panel_names_rejected_on_commit(
    app_cfg: Config[AppConfig],
    whitelist: bool,
) -> None:
    before = Path(app_cfg.path).read_bytes()
    document = app_cfg.view()
    with pytest.raises(SchemaValidationError, match="Duplicate panel name 'local'"):
        with app_cfg.edit() as tx:
            tx.view()["3xui"]["second"] = {
                **document["3xui"]["local"],
                "whitelist": whitelist,
            }
    assert Path(app_cfg.path).read_bytes() == before
    assert app_cfg.view() == document


@pytest.mark.parametrize("initial_load", [False, True])
def test_duplicate_panel_names_rejected_on_load(
    app_cfg: Config[AppConfig],
    initial_load: bool,
) -> None:
    document = app_cfg.view()
    panels = document["3xui"]
    panels["second"] = {**panels["local"]}
    path = Path(app_cfg.path)
    path.write_text(json.dumps(document), encoding="utf-8")
    if initial_load:
        with pytest.raises(SchemaValidationError, match="Duplicate panel name 'local'"):
            Config[AppConfig](
                path=path,
                schema_path=app_cfg.schema_path,
                validators=(check_config_document,),
                backup_dir=None,
                sync_mode="none",
            )
    else:
        with pytest.raises(SchemaValidationError, match="Duplicate panel name 'local'"):
            app_cfg.reload()


def test_distinct_panel_names_accepted(app_cfg: Config[AppConfig]) -> None:
    with app_cfg.edit() as tx:
        panels = tx.view()["3xui"]
        panels["second"] = {**panels["local"], "name": "second"}
    app_cfg.reload()
    assert app_cfg.view()["3xui"]["second"]["name"] == "second"


@pytest.mark.parametrize("length", [0, 19, 20])
def test_admin_token_minimum_length(app_cfg: Config[AppConfig], length: int) -> None:
    if length < 20:
        with pytest.raises(SchemaValidationError):
            with app_cfg.edit() as tx:
                tx["api_token"] = "x" * length
    else:
        with app_cfg.edit() as tx:
            tx["api_token"] = "x" * length
        assert app_cfg.view()["api_token"] == "x" * length


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"telegram": ["English", "Русский"]},
        {"discord": ["English", "Русский"]},
        {"telegram": ["", ""], "discord": ["English", "Русский"]},
    ],
)
def test_support_text_accepts_optional_platforms(
    app_cfg: Config[AppConfig],
    value: JsonValue,
) -> None:
    assert "support_text" not in app_cfg.view()
    assert "discord" not in app_cfg.view()
    with app_cfg.edit() as tx:
        tx["support_text"] = value
    assert app_cfg.copy()["support_text"] == value


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        "support",
        {"unknown": ["en", "ru"]},
        {"telegram": []},
        {"discord": ["en"]},
        {"telegram": ["en", "ru", "extra"]},
        {"discord": ["en", 1]},
        {"telegram": None},
    ],
)
def test_support_text_rejects_invalid_overrides(
    app_cfg: Config[AppConfig],
    value: JsonValue,
) -> None:
    with pytest.raises(SchemaValidationError):
        with app_cfg.edit() as tx:
            tx["support_text"] = value
    assert "support_text" not in app_cfg.view()


def test_panel_headers_reject_non_string_values(app_cfg: Config[AppConfig]) -> None:
    document = app_cfg.copy()
    panels = cast(JsonDict, document["3xui"])
    panel = cast(JsonDict, panels["local"])
    panel["inject_headers"] = {"X-Test": 42}
    with pytest.raises(SchemaValidationError):
        app_cfg.validate_document(document)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("api_admin_ui_auth", ["admin", 1]),
        ("api_admin_ui_auth", ["admin", "password", "extra"]),
        ("api_admin_ui_auth", ["admin"]),
        ("panel_alert_cooldown", True),
        ("fingerprints", []),
        ("fingerprints", ["unknown"]),
        ("fingerprints", ["chrome", "unknown"]),
        ("fingerprints", [""]),
        ("fingerprints", ["Chrome"]),
        ("fingerprints", [" chrome"]),
        ("fingerprints", [360]),
        ("fingerprints", "chrome"),
        ("fingerprints", ["chrome", "chrome"]),
        ("redis", {"url": "redis://localhost/0", "socket_timeout": 0}),
        ("redis", {"url": "redis://localhost/0", "socket_timeout": True}),
        ("unexpected", "value"),
    ],
)
def test_schema_constraints_reject_invalid_commit(
    app_cfg: Config[AppConfig],
    key: str,
    value: JsonValue,
) -> None:
    before = Path(app_cfg.path).read_bytes()
    with pytest.raises(SchemaValidationError):
        with app_cfg.edit() as tx:
            tx[key] = value
    assert Path(app_cfg.path).read_bytes() == before


@pytest.mark.parametrize(
    "fingerprint",
    ["chrome", "safari", "edge", "ios", "firefox", "android", "360", "qq", "random", "randomized"],
)
def test_schema_accepts_allowed_fingerprints(app_cfg: Config[AppConfig], fingerprint: str) -> None:
    with app_cfg.edit() as tx:
        tx["fingerprints"] = [fingerprint]
    assert app_cfg["fingerprints"] == [fingerprint]


@pytest.mark.parametrize("fingerprints", [[], ["unknown"], ["chrome", "unknown"]])
def test_schema_rejects_invalid_fingerprints_on_load(
    app_cfg: Config[AppConfig],
    fingerprints: list[str],
) -> None:
    path = Path(app_cfg.path)
    document = app_cfg.copy()
    document["fingerprints"] = fingerprints
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SchemaValidationError, match="fingerprints"):
        Config[AppConfig](
            path=path,
            schema_path=app_cfg.schema_path,
            backup_dir=None,
            sync_mode="none",
        )
