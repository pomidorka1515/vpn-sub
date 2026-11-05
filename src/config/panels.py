from __future__ import annotations

from typing import TYPE_CHECKING

from errors import SchemaValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .constants import JsonValue


def validate_panel_limit(data: Mapping[str, JsonValue]) -> None:
    """Require unique panel names and at most one whitelist panel."""
    panels = data.get("3xui")
    if not isinstance(panels, dict):
        return
    panel_names: dict[str, str] = {}
    for key, value in panels.items():
        if not isinstance(value, dict):
            continue
        name = value.get("name")
        if not isinstance(name, str):
            continue
        if name in panel_names:
            raise SchemaValidationError(
                f"Duplicate panel name {name!r}; configured in: {panel_names[name]}, {key}",
            )
        panel_names[name] = key
    names = [
        key
        for key, value in panels.items()
        if isinstance(value, dict) and value.get("whitelist") is True
    ]
    if len(names) > 1:
        raise SchemaValidationError(
            f"At most one whitelist panel is allowed; configured: {', '.join(names)}",
        )
