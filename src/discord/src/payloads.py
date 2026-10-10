"""Narrow raw API payloads before formatting or charting them."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    import discord


class ResponseOptions(TypedDict, total=False):
    ephemeral: bool
    content: str
    view: discord.ui.View
    file: discord.File


def obj_map(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in cast(Mapping[object, object], value).items()}


def number(value: object) -> int | float:
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0
    return 0


def object_rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [
        obj_map(cast(object, item))
        for item in cast(list[object], value)
        if isinstance(item, Mapping)
    ]
