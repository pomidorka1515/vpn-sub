from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from config import AppConfig, Config, JsonDict, JsonValue
from errors import SchemaValidationError


@pytest.mark.parametrize("boundary", ["load", "reload", "commit", "validate"])
@pytest.mark.parametrize(
    ("field", "value"),
    cast(
        list[tuple[str, JsonValue]],
        [
            ("profiles.profile.json", {}),
            ("profiles.profile.json.settings", None),
            ("profiles.profile.json.settings", {}),
            ("profiles.profile.json.settings.vnext", None),
            ("profiles.profile.json.settings.vnext", []),
            ("profiles.profile.json.settings.vnext", [{}]),
            ("profiles.profile.json.settings.vnext", [{"users": []}]),
            ("profiles.profile.json.settings.vnext", [{"users": [None]}]),
            ("profiles.profile.json.streamSettings", None),
            ("profiles.profile.json.streamSettings.tlsSettings", []),
            ("profiles.profile.json.streamSettings.realitySettings", "invalid"),
            ("profiles.profile.json.streamSettings.xhttpSettings", False),
            ("profiles.profile.json.streamSettings.grpcSettings", 1),
            ("profiles.profile.json.streamSettings.httpupgradeSettings", []),
            ("profiles.profile.json.streamSettings.wsSettings", []),
            ("profiles.profile.json.streamSettings.wsSettings", {"headers": None}),
            ("json_template.outbounds", []),
            ("json_template.outbounds", {}),
            ("json_template.outbounds", [None]),
            ("json_template.dns", []),
            ("json_template.inbounds", {}),
            ("json_template.remarks", None),
            ("json_template.routing", []),
            ("json_template.routing.rules", {}),
            ("json_template.routing.domainStrategy", []),
            ("json_template.meta", None),
        ],
    ),
)
def test_json_profile_schema_rejects_malformed_structures(
    app_cfg: Config[AppConfig],
    boundary: str,
    field: str,
    value: JsonValue,
) -> None:
    document = cast(JsonDict, json.loads(json.dumps(app_cfg.copy())))
    document["nodes"] = {"local": "example.test"}
    document["profiles"] = {
        "profile": {
            "flag": "",
            "name": ["local", "local"],
            "json": {"settings": {"vnext": [{"users": [{}]}]}, "streamSettings": {}},
            "description": ["", ""],
            "whitelist": False,
            "xhttpExtra": {},
            "masterLink": "",
            "node": "local",
            "shortProfileDescription": ["", ""],
        },
    }
    target = document
    *parents, key = field.split(".")
    for parent in parents:
        child = target[parent]
        assert isinstance(child, dict)
        target = child
    target[key] = value
    path = Path(app_cfg.path)
    before = path.read_bytes()

    def exercise_boundary() -> None:
        if boundary == "validate":
            app_cfg.validate_document(document)
        elif boundary == "commit":
            with app_cfg.edit() as tx:
                for key, item in document.items():
                    tx[key] = item
        else:
            path.write_text(json.dumps(document), encoding="utf-8")
            if boundary == "reload":
                app_cfg.reload()
            else:
                Config[AppConfig](path=path, schema_path=app_cfg.schema_path)

    with pytest.raises(SchemaValidationError):
        exercise_boundary()
    if boundary in {"commit", "validate"}:
        assert path.read_bytes() == before


@pytest.mark.parametrize(
    "stream",
    [
        {},
        {"tlsSettings": {}, "xhttpSettings": {}, "grpcSettings": {}},
        {"realitySettings": {}, "xhttpSettings": {}},
        {"wsSettings": {}, "httpupgradeSettings": {}},
        {"wsSettings": {"headers": {"Custom": "value"}}},
        {"tlsSettings": None, "realitySettings": None, "wsSettings": None},
    ],
)
def test_json_profile_schema_preserves_supported_transports(
    app_cfg: Config[AppConfig],
    stream: JsonDict,
) -> None:
    from builders.json_profiles import build_json

    document = cast(JsonDict, json.loads(json.dumps(app_cfg.copy())))
    document["nodes"] = {"local": "example.test"}
    document["profiles"] = {
        "profile": {
            "flag": "",
            "name": ["local", "local"],
            "json": {
                "protocol": "vless",
                "settings": {"vnext": [{"users": [{}]}]},
                "streamSettings": stream,
                "customExtension": {"enabled": True},
            },
            "description": ["", ""],
            "whitelist": False,
            "xhttpExtra": {},
            "masterLink": "",
            "node": "local",
            "shortProfileDescription": ["", ""],
        },
    }
    with app_cfg.edit() as tx:
        for key, item in document.items():
            tx[key] = item
    result = build_json(cast(AppConfig, app_cfg.copy()), "user-uuid", "en", "chrome")
    assert result[0]["remarks"] == "local"
    outbound = cast(JsonDict, cast(list[object], result[0]["outbounds"])[0])
    assert outbound["customExtension"] == {"enabled": True}
    assert outbound["settings"] == {
        "vnext": [{"users": [{"id": "user-uuid"}], "address": "example.test"}],
    }
