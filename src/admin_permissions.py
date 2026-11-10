from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterable

# The HTTP permission vocabulary is deliberately closed. Prefix matching always
# includes a dot boundary; bare branch names are not grants.
PERMISSIONS = (
    "auth.read.accounts",
    "auth.read.tokens",
    "auth.create.accounts",
    "auth.create.tokens",
    "auth.update.accounts",
    "auth.update.tokens",
    "user.read",
    "user.edit.displayname",
    "user.edit.username",
    "user.edit.fingerprint",
    "user.edit.time",
    "user.edit.bandwidth.monthly.used",
    "user.edit.bandwidth.monthly.limit",
    "user.edit.bandwidth.wl_monthly.used",
    "user.edit.bandwidth.wl_monthly.limit",
    "user.reset",
    "user.password_reset",
    "user.delete",
    "user.create",
    "code.read",
    "code.add.bonus",
    "code.add.register",
    "code.delete",
    "audit.read",
    "state.read.panels",
    "state.read.host",
    "state.charts.read",
    "snapshots.read",
    "health.read.recovery",
    "health.read.status",
    "health.read.online_users",
    "health.read.teapot",
    "health.sync",
    "leaderboard.read.total",
    "leaderboard.read.monthly",
    "leaderboard.read.wl_monthly",
    "config.read.main",
    "config.read.inbounds",
    "config.read.panels",
    "config.write.main",
    "config.write.inbounds",
    "config.write.panels",
)
BRANCHES = frozenset(
    ".".join(parts[:index])
    for permission in PERMISSIONS
    for parts in (permission.split("."),)
    for index in range(1, len(parts))
)
VALID_GRANTS = frozenset((*PERMISSIONS, "*", *(branch + ".*" for branch in BRANCHES)))
USER_FIELDS = {
    "displayname": "user.edit.displayname",
    "new_username": "user.edit.username",
    "fingerprint": "user.edit.fingerprint",
    "time": "user.edit.time",
    "used": "user.edit.bandwidth.monthly.used",
    "limit": "user.edit.bandwidth.monthly.limit",
    "wl_used": "user.edit.bandwidth.wl_monthly.used",
    "wl_limit": "user.edit.bandwidth.wl_monthly.limit",
}
ROOT_CONFIG_KEYS = frozenset(
    ("api_token", "api_admin_ui_auth", "salt", "bot", "publicbot", "discord", "redis")
)


def allows(grants: Iterable[str], permission: str) -> bool:
    return any(
        grant in {"*", permission} or (grant.endswith(".*") and permission.startswith(grant[:-1]))
        for grant in grants
    )


def prerequisite(permission: str) -> str | None:
    if permission.startswith("user.") and permission != "user.read":
        return "user.read"
    if permission.startswith("code.") and permission != "code.read":
        return "code.read"
    if permission.startswith(("auth.create.", "auth.update.")):
        return "auth.read." + permission.rsplit(".", 1)[1]
    if permission.startswith("config.write."):
        return permission.replace(".write.", ".read.")
    return None


def validate_grants(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("permissions must be an array of known permission strings")
    value = cast(list[object], value)
    if len(value) > len(VALID_GRANTS):
        raise ValueError("Too many permissions")
    if any(not isinstance(item, str) or item not in VALID_GRANTS for item in value):
        raise ValueError("Unknown permission")
    grants = sorted(set(cast(list[str], value)))
    for permission in PERMISSIONS:
        required = prerequisite(permission)
        if required and allows(grants, permission) and not allows(grants, required):
            raise ValueError(f"{permission} requires {required}")
    return [
        grant
        for grant in grants
        if not allows((other for other in grants if other != grant), grant)
    ]


def config_section(key: str) -> str:
    return {"profiles": "inbounds", "3xui": "panels"}.get(key, "main")


# A missing route entry fails closed, including any subsequently added route.
ROUTE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "user_list": ("user.read",),
    "user_info": ("user.read",),
    "fingerprints": ("user.read",),
    "user_history": ("user.read",),
    "user_add": ("user.read", "user.create"),
    "user_delete": ("user.read", "user.delete"),
    "user_reset": ("user.read", "user.reset"),
    "user_password_reset": ("user.read", "user.password_reset"),
    "user_update": ("user.read",),
    "user_refresh": ("health.sync",),
    "user_onlines": ("health.read.online_users",),
    "code_list": ("code.read",),
    "code_info": ("code.read",),
    "code_add": ("code.read",),
    "code_delete": ("code.read", "code.delete"),
    "audit": ("audit.read",),
    "panel_status": ("state.read.panels",),
    "system_status": ("state.read.host",),
    "polling_status": ("state.charts.read",),
    "full_info": ("state.read.panels", "state.read.host"),
    "snapshots": ("snapshots.read",),
    "leaderboard": (),
    "health": ("health.read.status",),
    "operation_status": ("health.read.recovery",),
    "operation_rollback_resolve": ("*",),
    "teapot": ("health.read.teapot",),
    "config_get": (),
    "config_set": (),
    "auth_catalog": (),
    "auth_list": (),
    "auth_create": (),
    "auth_update": (),
    "auth_delete": (),
}
