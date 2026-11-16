from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import cast

from .constants import JsonValue


def freeze(value: JsonValue) -> JsonValue:
    """Detach JSON containers into recursively read-only mappings and tuples."""
    if value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    return tuple(freeze(item) for item in value)


def _thaw(value: JsonValue) -> JsonValue:
    if value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    return [_thaw(item) for item in value]


def thaw[Doc](value: Doc) -> Doc:
    """Copy a JSON document/section into mutable dicts and lists.

    Retains the caller's document contract, including TypedDict field types.
    Unlike deepcopy, this also accepts immutable config snapshots.
    """
    return cast(Doc, _thaw(cast(JsonValue, value)))
