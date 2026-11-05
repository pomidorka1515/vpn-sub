from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from errors import ValidationError

from .panels import validate_panel_limit

if TYPE_CHECKING:
    from .constants import JsonValue

# pyright: reportUnnecessaryIsInstance=false


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def check_config_document(data: Mapping[str, JsonValue]) -> None:
    """Validate application invariants at every config boundary.

    Wrong types fall through to schema validation instead of raising TypeError.
    This does not tighten config.schema.json.
    """
    profiles = data.get("profiles")
    nodes = data.get("nodes")
    if isinstance(profiles, Mapping) and isinstance(nodes, Mapping):
        for profile_id, profile in profiles.items():
            if not isinstance(profile, Mapping):
                continue
            node = profile.get("node")
            if isinstance(node, str) and node not in nodes:
                raise ValidationError(f"profiles.{profile_id}.node {node!r} is not a key of nodes")

    publicbot = data.get("publicbot")
    if isinstance(publicbot, Mapping):
        token = publicbot.get("token")
        if not isinstance(token, str):
            raise ValidationError("publicbot.token must be a string")

    bot = data.get("bot")
    if isinstance(bot, Mapping):
        token = bot.get("token")
        if not isinstance(token, str):
            raise ValidationError("bot.token must be a string")

    if not _nonempty_str(data.get("api_token")):
        raise ValidationError("api_token must not be empty")

    auth = data.get("api_admin_ui_auth")
    if (
        isinstance(auth, list)
        and len(cast("list[object]", auth)) == 2
        and (
            not _nonempty_str(cast("list[object]", auth)[0])
            or not _nonempty_str(cast("list[object]", auth)[1])
        )
    ):
        raise ValidationError("api_admin_ui_auth entries must not be empty")

    panels = data.get("3xui")
    if isinstance(panels, Mapping):
        for panel_id, panel in panels.items():
            if isinstance(panel, Mapping) and panel.get("nginx_auth"):
                raise ValidationError(
                    f"3xui.{panel_id}.nginx_auth is incompatible with panel bearer authentication; "
                    "remove nginx_auth and disable Basic authentication for the panel API",
                )

    validate_panel_limit(data)
