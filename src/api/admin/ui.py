from __future__ import annotations

from typing import cast

from flask import Response, make_response, redirect, render_template, request

from api.admin._base import AdminApiMixin
from api.common import ResponseType, asset_version, web_lang_tables
from api.decorators.auth import ADMIN_UI_COOKIE, ADMIN_UI_SESSION_LEN, new_admin_ui_session
from api.decorators.rate_limit import rate_limit
from util import compare, err, ok


class UiRoutes(AdminApiMixin):
    """Admin HTML pages and cookie session."""

    def reg_handles(self) -> None:
        conf = self.cfg.view()
        prefix = '/' + '/'.join(p.strip('/ ') for p in (conf['uri'],) if p and p.strip('/ '))
        for path, handler in (
            ('/admin', 'admin_ui'),
            ('/admin/token', 'admin_token'),
            ('/admin/login', 'admin_login_page'),
            ('/admin/session', 'admin_session'),
            ('/admin/logout', 'admin_logout'),
        ):
            func = getattr(type(self), handler)
            url = '/' + '/'.join(p.strip('/') for p in (prefix, path) if p.strip('/'))
            methods = ['POST'] if handler in ('admin_session', 'admin_logout') else ['GET']
            if handler == 'admin_session':
                func = rate_limit(10)(func)
            func = func.__get__(self, type(self))
            self.app.add_url_rule(url, handler, func, methods=methods, strict_slashes=False)

    def admin_ui(self) -> ResponseType:
        if not self._admin_cookie_ok():
            return make_response(redirect(self._admin_login_path()))
        lang, strings, fallback = web_lang_tables(self.sub.res.lang_cfg, 'admin')
        conf = self.cfg.view()
        html = render_template(
            'admin.html',
            prefix='/' + '/'.join(p for p in conf['uri'].split('/') if p),
            asset_version=asset_version(),
            lang=lang,
            L=strings,
            L_en=fallback,
        )
        response = Response(html, mimetype='text/html')
        if request.args.get('lang', '').lower() in ('en', 'ru'):
            response.set_cookie('lang', lang, max_age=31536000, samesite='Lax', path='/')
        return response

    def admin_token(self) -> ResponseType:
        if not self._admin_cookie_ok():
            return err("Unauthorized", 401)
        return ok(obj={"token": self.token, "api_root": self.uri})

    def admin_session(self) -> ResponseType:
        content = request.get_json(silent=True)
        if not isinstance(content, dict):
            return err("Body must be a JSON dict.", 400)
        body = cast(dict[str, object], content)
        username = body.get("username")
        password = body.get("password")
        if (
            not isinstance(username, str) or not isinstance(password, str)
            or not username or not password
        ):
            return err("Invalid credentials.", 401)
        if not self._admin_credentials_ok(username, password):
            return err("Invalid credentials.", 401)
        session = new_admin_ui_session()
        self.sub.res.db.set_admin_ui_session(session)
        response, code = ok(msg="Successful login")
        response.set_cookie(
            key=ADMIN_UI_COOKIE,
            value=session,
            max_age=30 * 24 * 3600,
            httponly=True,
            secure=True,
            samesite="Lax",
            path=self._admin_cookie_path(),
        )
        return response, code

    def admin_logout(self) -> ResponseType:
        self.sub.res.db.set_admin_ui_session(None)
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
        return '/' + '/'.join(p.strip('/ ') for p in (conf['uri'],) if p and p.strip('/ '))

    def _admin_login_path(self) -> str:
        return self._admin_prefix().rstrip('/') + '/admin/login'

    def _admin_cookie_path(self) -> str:
        prefix = self._admin_prefix().rstrip('/')
        return (prefix + '/admin') if prefix else '/admin'

    def _admin_credentials_ok(self, username: str, password: str) -> bool:
        conf = self.cfg.view()
        configured = conf["api_admin_ui_auth"]
        expected_user = cast(object, configured[0])
        expected_password = cast(object, configured[1])
        if not isinstance(expected_user, str) or not isinstance(expected_password, str):
            return False
        return compare(username, expected_user) and compare(password, expected_password)

    def _admin_cookie_ok(self) -> bool:
        cookie = request.cookies.get(ADMIN_UI_COOKIE)
        if not cookie or len(cookie) != ADMIN_UI_SESSION_LEN:
            return False
        stored = self.sub.res.db.admin_ui_session()
        if not stored or len(stored) != ADMIN_UI_SESSION_LEN:
            return False
        return compare(cookie, stored)

    def admin_login_page(self) -> ResponseType:
        lang, strings, fallback = web_lang_tables(self.sub.res.lang_cfg, 'admin_auth')
        conf = self.cfg.view()
        html = render_template(
            'admin-auth.html',
            prefix='/' + '/'.join(p for p in conf['uri'].split('/') if p),
            asset_version=asset_version(),
            lang=lang,
            L=strings,
            L_en=fallback,
        )
        response = Response(html, mimetype='text/html')
        if request.args.get('lang', '').lower() in ('en', 'ru'):
            response.set_cookie('lang', lang, max_age=31536000, samesite='Lax', path='/')
        return response
