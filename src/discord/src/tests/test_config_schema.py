from __future__ import annotations

import json
from typing import cast

import jsonschema
import pytest

from discord_helpers import DISCORD_ROOT, EXAMPLE_PATH, SCHEMA_PATH


@pytest.fixture
def schema() -> dict[str, object]:
    loaded: object = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)


def test_example_validates(schema: dict[str, object]) -> None:
    example = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(example, schema)


def test_private_rejects_extra_keys(schema: dict[str, object]) -> None:
    data: dict[str, dict[str, object]] = {
        "public": {"token": "x"},
        "private": {"token": "nope", "whitelist": [], "api_token": "t"},
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, schema)


def test_private_requires_whitelist_and_api_token(schema: dict[str, object]) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"public": {"token": "x"}, "private": {}}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"public": {"token": "x"}, "private": {"whitelist": []}}, schema)
    jsonschema.validate(
        {"public": {"token": "x"}, "private": {"whitelist": [1], "api_token": "t"}},
        schema,
    )


def test_public_requires_token(schema: dict[str, object]) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"public": {}, "private": {"whitelist": [], "api_token": ""}}, schema)


def test_extra_top_level_keys_fail(schema: dict[str, object]) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"public": {"token": ""}, "private": {"whitelist": [], "api_token": ""}, "extra": 1},
            schema,
        )



def test_example_schema_path_resolves_from_data_dir() -> None:
    example = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    schema_ref = example["$schema"]
    assert isinstance(schema_ref, str)
    data_config = DISCORD_ROOT.parents[1] / "data" / "discord.json"
    resolved = (data_config.parent / schema_ref).resolve()
    assert resolved == SCHEMA_PATH.resolve()
