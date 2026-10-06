from ._common import Decorated, DecoratedInject, WrappedReturn
from typing import cast, TYPE_CHECKING
from flask import request
from functools import wraps
from util import err, compare

import secrets

if TYPE_CHECKING:
    from ..admin._base import AdminApiMixin
    from ..web import WebApi

ADMIN_UI_COOKIE = "admin_ui"
ADMIN_UI_SESSION_LEN = 100

def new_admin_ui_session() -> str:
    """Random session id for the single admin UI account. Not derived from credentials."""
    return secrets.token_hex(ADMIN_UI_SESSION_LEN // 2)

def requires_admin_auth[API_T: AdminApiMixin, **P, R](
    f: Decorated[API_T, P, R],
) -> Decorated[API_T, P, R]:
    """
    Admin API auth via Authorization header. Returns 401 on failure."""
    @wraps(f)
    def wrapper(self: API_T, *args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]:
        provided = request.headers.get('Authorization', '')
        if not provided or not compare(provided, self.token):
            return err("Unauthorized", 401)
        return f(self, *args, **kwargs)
    return cast(Decorated[API_T, P, R], wrapper)
def requires_webapi_auth[**P, R](f: DecoratedInject[WebApi, str, P, R]) -> Decorated[WebApi, P, R]:
    """WebApi auth via token cookie. Injects `username` as first arg after self.
    Returns 401 on failure."""
    @wraps(f)
    def wrapper(self: WebApi, *args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]:
        auth_token = request.cookies.get('auth_token')
        username = self.validate_auth_token(auth_token)
        if not username:
            return err("Invalid auth token.", 401)
        return f(self, username, *args, **kwargs)
    return cast("Decorated[WebApi, P, R]", wrapper)
def requires_no_auth[**P, R](f: Decorated[WebApi, P, R]) -> Decorated[WebApi, P, R]:
    """WebApi: reject if already authenticated (for register). Returns 403."""
    @wraps(f)
    def wrapper(self: WebApi, *args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]:
        auth_token = request.cookies.get('auth_token')
        if auth_token and self.validate_auth_token(auth_token):
            return err("Must not be authorized.", 403)
        return f(self, *args, **kwargs)
    return cast("Decorated[WebApi, P, R]", wrapper)
