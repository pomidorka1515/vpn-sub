from __future__ import annotations

from dataclasses import asdict
from typing import ClassVar
from uuid import uuid4

from flask import Response, g, request

from api.common import ResponseType, Route
from api.decorators import requires_fields_strict, requires_no_auth, requires_webapi_auth
from api.web._base import WebApiMixin
from util import err, generate_token, ok, sanitize


class SessionRoutes(WebApiMixin):
    """Registration, login, logout, and account deletion."""

    ROUTES: ClassVar[tuple[Route, ...]] = (
        Route('POST', '/webapi/register', 'register', 5),
        Route('POST', '/webapi/login', 'login', 10),
        Route('POST', '/webapi/logout', 'logout', 20),
        Route('POST', '/webapi/delete', 'delete', 3),
        Route('GET', '/webapi/validate', 'validate_username', 80),
    )

    def validate_auth_token(self, auth_token: str | None = None) -> str | None:
        def _v(token: str | None) -> str | None:
            if not token or len(token) != 100:
                return None
            username = self.sub.user_svc.auth_token_to_user(token)
            return username if isinstance(username, str) else None

        if auth_token is not None:
            return _v(auth_token)
        return _v(request.cookies.get('auth_token'))

    def _clear_auth_cookies(self, response: Response) -> None:
        for name in ('auth_token', 'token'):
            response.set_cookie(
                name,
                '',
                max_age=0,
                httponly=True,
                secure=True,
                samesite='Lax'
            )

    def validate_credentials(self, username: str, password: str) -> str | bool:
        """Validate username+password. Returns internal username on success."""
        return self.sub.password_svc.validate_credentials(username, password) or False

    def validate_username(self) -> ResponseType:
        """Check if a username is valid (no illegal chars)"""
        raw = request.args.get('username', None)
        if not raw:
            return err("'username' field is missing")
        sanitized = sanitize(raw, "external")
        valid = raw == sanitized and len(raw) > 0
        taken = self.sub.user_svc.external_username_exists(sanitized)
        return ok(obj={
            "MAX_LENGTH": 32,
            "valid": valid,
            "taken": taken,
            "sanitized": sanitized
        })

    @requires_webapi_auth
    @requires_fields_strict(('current_password', str))
    def delete(self, username: str) -> ResponseType:
        content = g.json_obj
        current_password: str = content.get('current_password')
        cur_ext = self.sub.user_svc.get_external_username(username)
        if not cur_ext:
            return err("Account has no credentials set", 400)
        if self.sub.password_svc.validate_credentials(cur_ext, current_password) != username:
            return err("Invalid current password", 401)
        self.sub.user_svc.set_auth_token(username, None)
        self.sub.business_svc.delete_user(username=username, perma=True)

        resp, code = ok(msg="Deleted account")
        self._clear_auth_cookies(resp)
        return resp, code

    @requires_webapi_auth
    def logout(self, username: str) -> ResponseType:
        resp, code = ok(msg="Logged out")
        self.sub.user_svc.set_auth_token(username, None)
        self._clear_auth_cookies(resp)
        return resp, code

    @requires_no_auth
    @requires_fields_strict(
        ('username', str),
        ('password', str),
        ('code', str),
        ('name', str)
    )
    def register(self) -> ResponseType:
        """Register a new user via code."""
        content = g.json_obj
        raw_name: str = content.get('name')
        raw_username: str = content.get('username')
        raw_password: str = content.get('password')
        raw_code: str = content.get('code')

        if not (1 <= len(raw_name) <= 16):
            return err("'name' must be 1-16 characters")
        if not (1 <= len(raw_username) <= 32):
            return err("'username' must be 1-32 characters")
        if not (1 <= len(raw_password) <= 128):
            return err("'password' must be 1-128 characters")
        if not (1 <= len(raw_code) <= 64):
            return err("'code' must be 1-64 characters")

        result = self.sub.business_code_svc.register_with_code(
            code=raw_code,
            username=f"web_{uuid4().hex[:16]}",
            displayname=raw_name,
            ext_username=raw_username,
            ext_password=raw_password,
        )
        return ok("Created", 201, obj=asdict(result))

    @requires_fields_strict(
        ('username', str),
        ('password', str)
    )
    def login(self) -> ResponseType:
        """Get the cookie for auth."""
        content = g.json_obj
        internal = self.validate_credentials(content['username'], content['password'])
        if not isinstance(internal, str):
            return err("Invalid credentials.", 401)
        auth_token = generate_token("auth")
        self.sub.user_svc.set_auth_token(internal, auth_token)
        r, code = ok(msg="Successful login", obj={"username": content['username']})
        r.set_cookie(
            key='auth_token',
            value=auth_token,
            max_age=30 * 24 * 3600,
            httponly=True,
            secure=True,
            samesite='Lax'
        )
        r.set_cookie(
            key='token',
            value='',
            max_age=0,
            httponly=True,
            secure=True,
            samesite='Lax'
        )
        return r, code
