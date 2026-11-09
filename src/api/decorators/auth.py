from __future__ import annotations

import hmac
import json
import secrets
from functools import wraps
from typing import TYPE_CHECKING, cast

from quart import g, request

from loggers import Logger
from util import compare, err

from ._common import Decorated

if TYPE_CHECKING:
    from ..admin._base import AsyncAdminApiMixin
    from ..web._base import AsyncWebApiMixin
    from ._common import DecoratedInject, WrappedReturn

ADMIN_UI_COOKIE = "admin_ui"
ADMIN_UI_SESSION_LEN = 100
log = Logger("admin_auth")


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
    return "ui." + hmac.digest(api.ui_session_key, ("api:" + session).encode(), "sha256").hex()


def requires_admin_auth[API_T: AsyncAdminApiMixin, **P, R](
    f: Decorated[API_T, P, R],
) -> Decorated[API_T, P, R]:
    """
    Admin API auth with a shared per-IP failed-attempt lockout."""

    @wraps(f)
    async def wrapper(self: API_T, *args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]:
        provided = request.headers.get("Authorization", "")
        valid = bool(provided) and compare(provided, self.token)
        g.admin_browser_auth = False
        if not valid and provided.startswith("ui."):
            session = await valid_admin_ui_session(self)
            valid = session is not None and compare(provided, admin_browser_token(self, session))
            g.admin_browser_auth = valid
        try:
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
