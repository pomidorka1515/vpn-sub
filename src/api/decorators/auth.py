from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from functools import wraps
from typing import TYPE_CHECKING, cast

from quart import g, request

from admin_permissions import (
    ROOT_CONFIG_KEYS,
    ROUTE_PERMISSIONS,
    USER_FIELDS,
    allows,
    config_section,
)
from loggers import Logger
from util import compare, err

from ._common import Decorated

if TYPE_CHECKING:
    from quart import Response

    from db.admin import AdminIdentity

    from ..admin._base import AsyncAdminApiMixin
    from ..web._base import AsyncWebApiMixin
    from ._common import DecoratedInject, WrappedReturn

ADMIN_UI_COOKIE = "admin_ui"
ADMIN_UI_SESSION_LEN = 100
log = Logger("admin_auth")


def credential_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _scoped_signature(api: AsyncAdminApiMixin, nonce: str) -> str:
    return hmac.digest(api.ui_session_key, ("scoped-session:" + nonce).encode(), "sha256").hex()[
        :32
    ]


def new_scoped_session(api: AsyncAdminApiMixin) -> str:
    nonce = secrets.token_hex(32)
    return "sa." + nonce + _scoped_signature(api, nonce)


def valid_scoped_cookie(api: AsyncAdminApiMixin, cookie: str) -> bool:
    return (
        len(cookie) == 99
        and cookie.startswith("sa.")
        and compare(cookie[67:], _scoped_signature(api, cookie[3:67]))
    )


def _scoped_bearer_prefix(api: AsyncAdminApiMixin) -> str:
    return (
        "ui."
        + hmac.digest(api.ui_session_key, b"scoped-browser-process", "sha256").hex()[:16]
        + "."
    )


def permission_denied(permission: str) -> tuple[Response, int]:
    return err(f"Missing {permission} permission", 403, {"required_permission": permission})


async def resolve_admin(
    api: AsyncAdminApiMixin, provided: str
) -> tuple[tuple[str, ...], AdminIdentity | None, bool] | None:
    if provided and compare(provided, api.token):
        return ("*",), None, False
    provided = provided.removeprefix("Bearer ")
    if provided and compare(provided, api.token):
        return ("*",), None, False
    if provided.startswith("ui."):
        session = await valid_admin_ui_session(api)
        if session is not None and compare(provided, admin_browser_token(api, session)):
            return ("*",), None, True
        if not provided.startswith(_scoped_bearer_prefix(api)):
            return None
    if not provided or len(provided) > 512:
        return None
    identity = await api.sub.res.db.admin_bearer_identity(credential_digest(provided))
    if identity is None:
        return None
    return identity.permissions, identity, identity.kind == "accounts"


async def required_permissions(handler: str) -> tuple[str, ...]:
    required = ROUTE_PERMISSIONS.get(handler, ("*",))
    content = await request.get_json(silent=True) if request.method == "POST" else None
    body = cast(dict[str, object], content) if isinstance(content, dict) else {}
    if handler == "user_update":
        required += tuple(permission for field, permission in USER_FIELDS.items() if field in body)
    elif (
        handler == "code_add"
        and isinstance(body.get("action"), str)
        and body.get("action") in {"bonus", "register"}
    ):
        required += (f"code.add.{body['action']}",)
    elif (
        handler == "leaderboard"
        and isinstance(body.get("type"), str)
        and body.get("type") in {"total", "monthly", "wl_monthly"}
    ):
        required += (f"leaderboard.read.{body['type']}",)
    elif handler == "config_set":
        values = body.get("values")
        if isinstance(values, dict):
            for key in cast(dict[str, object], values):
                required += (f"config.write.{config_section(key)}",)
                if key in ROOT_CONFIG_KEYS:
                    required += ("*",)
    elif handler == "config_get":
        if not any(
            allows(g.admin_permissions, f"config.read.{section}")
            for section in ("main", "inbounds", "panels")
        ):
            required += ("config.read.main",)
    elif handler in {"auth_list", "auth_create", "auth_update", "auth_delete"}:
        kind = request.args.get("kind") if handler == "auth_list" else body.get("kind")
        if isinstance(kind, str) and kind in {"accounts", "tokens"}:
            action = {
                "auth_list": "read",
                "auth_create": "create",
                "auth_update": "update",
                "auth_delete": "update",
            }[handler]
            required += (f"auth.read.{kind}", f"auth.{action}.{kind}")
    return required


def _session_signature(api: AsyncAdminApiMixin, nonce: str) -> str:
    credentials = api.cfg.view()["api_admin_ui_auth"]
    message = json.dumps([nonce, credentials], ensure_ascii=False).encode()
    return hmac.digest(api.ui_session_key, message, "sha256").hex()[:36]


def new_admin_ui_session(api: AsyncAdminApiMixin) -> str:
    """Bind a random session to current credentials and this serving process."""
    nonce = secrets.token_hex(32)
    return nonce + _session_signature(api, nonce)


async def valid_admin_ui_session(api: AsyncAdminApiMixin) -> str | None:
    session = await api.sub.res.db.admin_ui_session()
    if not session or len(session) != ADMIN_UI_SESSION_LEN:
        return None
    if not compare(session[64:], _session_signature(api, session[:64])):
        return None
    return session


def admin_browser_token(api: AsyncAdminApiMixin, session: str) -> str:
    """Domain-separated bearer/CSRF credential; never exposes the UI cookie."""
    prefix = _scoped_bearer_prefix(api) if session.startswith("sa.") else "ui."
    return prefix + hmac.digest(api.ui_session_key, ("api:" + session).encode(), "sha256").hex()


def requires_admin_auth[API_T: AsyncAdminApiMixin, **P, R](
    f: Decorated[API_T, P, R],
) -> Decorated[API_T, P, R]:
    """
    Admin API auth with a shared per-IP failed-attempt lockout."""

    @wraps(f)
    async def wrapper(self: API_T, *args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]:
        try:
            principal = await resolve_admin(self, request.headers.get("Authorization", ""))
            valid = principal is not None
            result = await self.limiter.admin_auth(request.remote_addr or "<unknown>", valid=valid)
        except Exception:
            log.error("admin auth store failed")  # ruff: ignore[error-instead-of-exception] credentials may occur in exception text
            return err("Authentication temporarily unavailable.", 503)
        if result > 0:
            response, code = err("Too many authentication attempts.", 429)
            response.headers["Retry-After"] = str(result)
            return response, code
        if result != 0:
            return err("Unauthorized", 401)
        if principal is None:
            return err("Unauthorized", 401)
        g.admin_permissions, g.admin_identity, g.admin_browser_auth = principal
        for permission in await required_permissions(f.__name__):
            if not allows(g.admin_permissions, permission):
                return permission_denied(permission)
        return await f(self, *args, **kwargs)

    return cast(Decorated[API_T, P, R], wrapper)


def requires_webapi_auth[API_T: AsyncWebApiMixin, **P, R](
    f: DecoratedInject[API_T, str, P, R],
) -> Decorated[API_T, P, R]:
    """WebApi auth via token cookie. Injects `username` as first arg after self.
    Returns 401 on failure."""

    @wraps(f)
    async def wrapper(self: API_T, *args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]:
        auth_token = request.cookies.get("auth_token")
        username = await self.validate_auth_token(auth_token)
        if not username:
            return err("Invalid auth token.", 401)
        return await f(self, username, *args, **kwargs)

    return cast(Decorated[API_T, P, R], wrapper)


def requires_no_auth[API_T: AsyncWebApiMixin, **P, R](
    f: Decorated[API_T, P, R],
) -> Decorated[API_T, P, R]:
    """WebApi: reject if already authenticated (for register). Returns 403."""

    @wraps(f)
    async def wrapper(self: API_T, *args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]:
        auth_token = request.cookies.get("auth_token")
        if auth_token and await self.validate_auth_token(auth_token):
            return err("Must not be authorized.", 403)
        return await f(self, *args, **kwargs)

    return cast(Decorated[API_T, P, R], wrapper)
