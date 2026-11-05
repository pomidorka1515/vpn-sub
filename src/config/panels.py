from __future__ import annotations

from typing import TYPE_CHECKING

from errors import SchemaValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .constants import JsonValue


def validate_panel_limit(data: Mapping[str, JsonValue]) -> None:
    """Allow at most one panel dedicated to whitelist traffic."""
    panels = data.get("3xui")
    if not isinstance(panels, dict):
        return
    names = [
        key
        for key, value in panels.items()
        if isinstance(value, dict) and value.get("whitelist") is True
    ]
    if len(names) > 1:
        raise SchemaValidationError(
            f"At most one whitelist panel is allowed; configured: {', '.join(names)}"
        )
