from __future__ import annotations

from typing import TYPE_CHECKING, cast

from argon2.exceptions import InvalidHashError, VerificationError
from quart import Response, g, make_response, redirect, render_template, request, send_file  # pyright: ignore[reportUnknownVariableType]  # Upstream PathLike lacks a type argument.

from admin_permissions import PERMISSIONS, prerequisite
from api.admin._base import AsyncAdminApiMixin
from api.common import RES_DIR, asset_version, web_lang_tables
from api.decorators.auth import (
    ADMIN_UI_COOKIE,
    ADMIN_UI_SESSION_LEN,
    admin_browser_token,
    credential_digest,
    new_admin_ui_session,
    new_scoped_session,
    valid_admin_ui_session,
    valid_scoped_cookie,
)
from api.decorators.rate_limit import rate_limit
from constants import SESSION_MAX_AGE
from util import compare, err, ok

if TYPE_CHECKING:
    from api.common import ResponseType


class AsyncUiRoutes(AsyncAdminApiMixin):
    """Admin HTML pages and cookie session."""

    def reg_handles(self) -> None:
        ui_routes = (
            ("/admin", "admin_ui"),
            ("/admin/main.js", "admin_js"),
            ("/admin/chart.umd.min.js", "admin_chart_js"),
            ("/admin/vendor/codemirror-5.65.16.min.js", "admin_codemirror_js"),
            ("/admin/main.css", "admin_css"),
            ("/admin/charts.css", "admin_charts_css"),
            ("/admin/vendor/codemirror-5.65.16.min.css", "admin_codemirror_css"),
            ("/admin/token", "admin_token"),
            ("/admin/login", "admin_login_page"),
            ("/admin/session", "admin_session"),
            ("/admin/logout", "admin_logout"),
        )
        self._no_store_endpoints = frozenset(
            [*(route.handler for route in self.ROUTES), *(handler for _, handler in ui_routes)]
        )
        self.app.after_request(self._admin_no_store)
        conf = self.cfg.view()
        prefix = "/" + "/".join(p.strip("/ ") for p in (conf["uri"],) if p and p.strip("/ "))
        for path, handler in ui_routes:
            func = getattr(type(self), handler)
            url = "/" + "/".join(p.strip("/") for p in (prefix, path) if p.strip("/"))
            methods = ["POST"] if handler in {"admin_session", "admin_logout"} else ["GET"]
            if handler in {"admin_session", "admin_logout"}:
                func = rate_limit(10)(func)
            func = func.__get__(self, type(self))
            self.app.add_url_rule(url, handler, func, methods=methods, strict_slashes=False)

    async def _admin_no_store(self, response: Response) -> Response:
        # Apply after error handling too, including rate-limit and storage failures.
        if request.endpoint in self._no_store_endpoints:
            response.headers["Cache-Control"] = "no-store"
        return response

    async def admin_ui(self) -> ResponseType:
        if not await self._admin_cookie_ok():
            return await make_response(redirect(self._admin_login_path()))
        lang, strings, fallback = web_lang_tables(self.sub.res.lang_cfg, "admin")
        conf = self.cfg.view()
        html = await render_template(
            "admin.html",
            prefix="/" + "/".join(p for p in conf["uri"].split("/") if p),
            chart_prefix=self._admin_cookie_path(),
            asset_version=await self.sub.res.offloads.files.run(asset_version),
            lang=lang,
            L=strings,
            L_en=fallback,
            L_ru=web_lang_tables(self.sub.res.lang_cfg, "admin", "ru")[1],
        )
        response = Response(html, mimetype="text/html")
        if request.args.get("lang", "").lower() in {"en", "ru"}:
            response.set_cookie("lang", lang, max_age=31536000, samesite="Lax", path="/")
        return response

    async def admin_js(self) -> ResponseType:
        return await self._admin_asset("assets/admin/main.js", "text/javascript")

    async def admin_chart_js(self) -> ResponseType:
        return await self._admin_asset("vendor/chart.umd.min.js", "text/javascript")

    async def admin_codemirror_js(self) -> ResponseType:
        return await self._admin_asset("vendor/codemirror-5.65.16.min.js", "text/javascript")

    async def admin_css(self) -> ResponseType:
        return await self._admin_asset("assets/admin.css", "text/css")

    async def admin_charts_css(self) -> ResponseType:
        return await self._admin_asset("assets/charts.css", "text/css")

    async def admin_codemirror_css(self) -> ResponseType:
        return await self._admin_asset("vendor/codemirror-5.65.16.min.css", "text/css")

    async def _admin_asset(self, name: str, mimetype: str) -> ResponseType:
        if not await self._admin_cookie_ok():
            return err("Unauthorized", 401)
        return await send_file(
            RES_DIR / name,
            mimetype=mimetype,
            add_etags=False,
            cache_timeout=0,
        )

    async def admin_token(self) -> ResponseType:
        if not await self._admin_cookie_ok():
            return err("Unauthorized", 401)
        return ok(
            obj={
                "token": admin_browser_token(self, request.cookies[ADMIN_UI_COOKIE]),
                "api_root": self.uri,
                "panels_configured": bool(self.sub.panels),
                "permissions": list(self._cookie_permissions),
                "catalog": [
                    {"permission": permission, "requires": prerequisite(permission)}
                    for permission in PERMISSIONS
                ],
            },
        )

    async def admin_session(self) -> ResponseType:
        content = await request.get_json(silent=True)
        if not isinstance(content, dict):
            return err("Body must be a JSON dict.", 400)
        body = cast(dict[str, object], content)
        username = body.get("username")
        password = body.get("password")
        if (
            not isinstance(username, str)
            or not isinstance(password, str)
            or not username
            or not password
        ):
            return err("Invalid credentials.", 401)
        if self._admin_credentials_ok(username, password):
            session = new_admin_ui_session(self)
            await self.sub.res.db.set_admin_ui_session(session)
        else:
            identity = await self.sub.res.db.admin_login_identity(username)
            if identity is None or len(password) > 128:
                return err("Invalid credentials.", 401)
            try:
                await self.sub.res.offloads.passwords.run(
                    self.sub.res.password_hasher.verify, identity.secret, password
                )
            except InvalidHashError, VerificationError:
                return err("Invalid credentials.", 401)
            session = new_scoped_session(self)
            if not await self.sub.res.db.admin_start_session(
                identity,
                credential_digest(session),
                credential_digest(admin_browser_token(self, session)),
            ):
                return err("Credentials changed; retry login.", 409)
        response, code = ok(msg="Successful login")
        response.set_cookie(
            key=ADMIN_UI_COOKIE,
            value=session,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            secure=True,
            samesite="Lax",
            path=self._admin_cookie_path(),
        )
        return response, code

    async def admin_logout(self) -> ResponseType:
        if not await self._admin_cookie_ok():
            return err("Unauthorized", 401)
        expected = admin_browser_token(self, request.cookies[ADMIN_UI_COOKIE])
        if not compare(request.headers.get("X-Admin-CSRF", ""), expected):
            return err("Invalid CSRF token", 403)
        cookie = request.cookies[ADMIN_UI_COOKIE]
        if cookie.startswith("sa."):
            await self.sub.res.db.admin_end_session(credential_digest(cookie))
        else:
            await self.sub.res.db.set_admin_ui_session(None)
        response, code = ok(msg="Logged out")
        response.set_cookie(
            key=ADMIN_UI_COOKIE,
            value="",
            max_age=0,
            httponly=True,
            secure=True,
            samesite="Lax",
            path=self._admin_cookie_path(),
        )
        return response, code

    def _admin_prefix(self) -> str:
        conf = self.cfg.view()
        return "/" + "/".join(p.strip("/ ") for p in (conf["uri"],) if p and p.strip("/ "))

    def _admin_login_path(self) -> str:
        return self._admin_prefix().rstrip("/") + "/admin/login"

    def _admin_cookie_path(self) -> str:
        prefix = self._admin_prefix().rstrip("/")
        return (prefix + "/admin") if prefix else "/admin"

    def _admin_credentials_ok(self, username: str, password: str) -> bool:
        conf = self.cfg.view()
        configured = conf["api_admin_ui_auth"]
        expected_user = cast(object, configured[0])
        expected_password = cast(object, configured[1])
        if not isinstance(expected_user, str) or not isinstance(expected_password, str):
            return False
        return compare(username, expected_user) and compare(password, expected_password)

    async def _admin_cookie_ok(self) -> bool:
        cookie = request.cookies.get(ADMIN_UI_COOKIE)
        if cookie and cookie.startswith("sa.") and len(cookie) == 99:
            if not valid_scoped_cookie(self, cookie):
                return False
            identity = await self.sub.res.db.admin_cookie_identity(credential_digest(cookie))
            if identity is None:
                return False
            self._set_cookie_permissions(identity.permissions)
            return True
        if not cookie or len(cookie) != ADMIN_UI_SESSION_LEN:
            return False
        stored = await valid_admin_ui_session(self)
        if not stored or len(stored) != ADMIN_UI_SESSION_LEN:
            return False
        valid = compare(cookie, stored)
        if valid:
            self._set_cookie_permissions(("*",))
        return valid

    @staticmethod
    def _set_cookie_permissions(permissions: tuple[str, ...]) -> None:
        g.admin_cookie_permissions = permissions

    @property
    def _cookie_permissions(self) -> tuple[str, ...]:
        return cast(tuple[str, ...], g.admin_cookie_permissions)

    async def admin_login_page(self) -> ResponseType:
        lang, strings, fallback = web_lang_tables(self.sub.res.lang_cfg, "admin_auth")
        conf = self.cfg.view()
        html = await render_template(
            "admin-auth.html",
            prefix="/" + "/".join(p for p in conf["uri"].split("/") if p),
            asset_version=await self.sub.res.offloads.files.run(asset_version),
            lang=lang,
            L=strings,
            L_en=fallback,
            L_ru=web_lang_tables(self.sub.res.lang_cfg, "admin_auth", "ru")[1],
        )
        response = Response(html, mimetype="text/html")
        if request.args.get("lang", "").lower() in {"en", "ru"}:
            response.set_cookie("lang", lang, max_age=31536000, samesite="Lax", path="/")
        return response
