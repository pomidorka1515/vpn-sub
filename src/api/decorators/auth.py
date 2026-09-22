from ._common import Decorated, DecoratedInject, WrappedReturn
from typing import cast, TYPE_CHECKING
from flask import request, Response
from functools import wraps
from util import err, compare

from ..common import BaseApi
import base64
import binascii

if TYPE_CHECKING:
    from ..admin import Api
    from ..web import WebApi

def _parse_basic_auth(header: str) -> tuple[str, str] | None:
    """Parse 'Basic <base64>' header. 
    
    Returns:
        (username, password) or None
    """
    if not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:]).decode()
        if ":" not in decoded:
            return None
        user, pw = decoded.split(":", 1)
        return user, pw
    except (ValueError, UnicodeError, binascii.Error):
        return None

def requires_admin_auth[**P, R](f: Decorated[Api, P, R]) -> Decorated[Api, P, R]:
    """
    Admin API auth via Authorization header. Returns 401 on failure."""
    @wraps(f)
    def wrapper(self: Api, *args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]: 
        provided = request.headers.get('Authorization', '')
        if not provided or not compare(provided, self.token):
            return err("Unauthorized", 401)
        return f(self, *args, **kwargs)
    return cast(Decorated[BaseApi, P, R], wrapper)
def requires_basic_admin_auth[**P, R](f: Decorated[Api, P, R]) -> Decorated[Api, P, R]:
    """Admin API auth via Basic auth header. Returns 401 on failure."""
    @wraps(f)
    def wrapper(self: Api, *args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]:
        provided = request.headers.get("Authorization", "")
        creds = _parse_basic_auth(provided)
        valid: tuple[str, str] = tuple(self.cfg["api_admin_ui_auth"])
        err = Response("Unauthorized")
        err.headers["WWW-Authenticate"] = 'Basic realm="Admin UI"'
        if not creds:
            return err, 401
        user, pw = creds
        if (not compare(user, valid[0])) or (not compare(pw, valid[1])):
            return err, 401
        return f(self, *args, **kwargs)
    return cast(Decorated[BaseApi, P, R], wrapper)
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
    return cast(Decorated[BaseApi, P, R], wrapper)
def requires_no_auth[**P, R](f: Decorated[WebApi, P, R]) -> Decorated[WebApi, P, R]:
    """WebApi: reject if already authenticated (for register). Returns 403."""
    @wraps(f)
    def wrapper(self: WebApi, *args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]:
        auth_token = request.cookies.get('auth_token')
        if auth_token and self.validate_auth_token(auth_token):
            return err("Must not be authorized.", 403)
        return f(self, *args, **kwargs)
    return cast(Decorated[BaseApi, P, R], wrapper)
