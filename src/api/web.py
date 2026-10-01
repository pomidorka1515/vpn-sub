from .common import (
    Route, BaseApi, RES_DIR, ResponseType, admin_module_names, asset_version, web_lang_tables,
)
from flask import Flask, request, Response, make_response, redirect, render_template, send_file, g
from config import ConfigLike
from core import Subscription
from bwatch import BWatch
from loggers import Logger
from util import ok, err, sanitize, make_qr, parse_bool, generate_token
from collections.abc import Callable
from typing import cast
from uuid import uuid4
from dataclasses import asdict
from .decorators import requires_fields_strict, requires_fields, requires_webapi_auth, requires_no_auth

__all__ = ["WebApi"]

def _admin_module_method(name: str) -> Callable[[WebApi], ResponseType]:
    def handler(self: WebApi) -> ResponseType:
        version = asset_version()
        source = (RES_DIR / 'admin' / f'{name}.js').read_text(encoding='utf-8')
        body = source.replace(".js';", f".js?v={version}';")
        return self._static(f'admin/{name}.js', 'text/javascript', body.encode('utf-8'))  # pyright: ignore[reportPrivateUsage]
    handler.__name__ = f'admin_{name}_js'
    return handler

class WebApi(BaseApi):
    """Public, user-facing API."""
    _admin_module = ''
    for _admin_module in admin_module_names():
        locals()[f'admin_{_admin_module}_js'] = _admin_module_method(_admin_module)
    del _admin_module

    ROUTES: list[Route] = [
        Route('GET', '/redirect', 'redirect_page'),
        Route('GET', '/common.js', 'common_js'),
        Route('GET', '/common.css', 'common_css'),
        Route('GET', '/auth.css', 'auth_css'),
        Route('GET', '/dashboard.css', 'dashboard_css'),
        Route('GET', '/history.css', 'history_css'),
        Route('GET', '/admin.css', 'admin_css'),
        *(Route('GET', f'/admin/{name}.js', f'admin_{name}_js') for name in admin_module_names()),
        Route('GET', '/dashboard.js', 'dashboard_js'),
        Route('GET', '/history.js', 'history_js'),
        Route('POST', '/webapi/register', 'register', 5),
        Route('POST', '/webapi/login', 'login', 10),
        Route('POST', '/webapi/bonus', 'bonus', 15),
        Route('GET', '/webapi/stats', 'stats', 20),
        Route('POST', '/webapi/reset', 'reset', 3),
        Route('POST', '/webapi/settings', 'settings', 15),
        Route('POST', '/webapi/logout', 'logout', 20),
        Route('GET', '/webapi/fingerprints', 'fps', None),
        Route('POST', '/webapi/delete', 'delete', 3),
        Route('GET', '/webapi/validate', 'validate_username', 80),
        Route('GET', '/webapi/profiles', 'profiles', 60),
        Route('GET', '/webapi/history', 'bandwidth_history', 30),
        Route('GET', '/panel', 'gui_panel'),
        Route('GET', '/auth', 'gui_auth'),
        Route('GET', '/history', 'gui_history'),
        Route('GET', '/webapi/qr', 'qr', 80)
    ]

    def __init__(self,
                 app: Flask,
                 cfg: ConfigLike,
                 sub: Subscription,
                 bw: BWatch):
        self.log = Logger(type(self).__name__)
        uri = '/' + '/'.join(p for p in cfg['uri'].split('/') if p)
        super().__init__(app, cfg, sub, bw, uri)
        self.prefix = self.uri.rstrip('/')

    def validate_auth_token(self, auth_token: str | None = None) -> str | None:
        def _v(token: str | None) -> str | None:
            if not token or len(token) != 100:
                return None
            username = self.sub.user_svc.auth_token_to_user(token)
            return username if isinstance(username, str) else None

        if auth_token is not None:
            return _v(auth_token)
        return _v(request.cookies.get('auth_token'))

    @staticmethod
    def _clear_auth_cookies(response: Response) -> None:
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
    def redirect_page(self) -> ResponseType:
        prefix = request.args.get('prefix', '')
        if not prefix.startswith(('happ://', 'v2ray', 'clash')):
            return err("Invalid prefix", 400)
        if len(prefix) > 512:
            return err("Prefix too long", 400)
        return make_response(send_file(RES_DIR / 'redirect.html', etag=False))

    def _static(self, name: str, mimetype: str, body: bytes | None = None) -> ResponseType:
        # url is versioned via ?v= (asset_version), so long caching is safe
        if body is None:
            response = make_response(send_file(RES_DIR / name, mimetype=mimetype, etag=True, max_age=31536000))
        else:
            response = make_response(body)
            response.mimetype = mimetype
            response.cache_control.max_age = 31536000
            response.cache_control.public = True
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        return response

    def common_js(self) -> ResponseType:
        return self._static('common.js', 'text/javascript')

    def common_css(self) -> ResponseType:
        return self._static('common.css', 'text/css')

    def auth_css(self) -> ResponseType:
        return self._static('auth.css', 'text/css')

    def dashboard_css(self) -> ResponseType:
        return self._static('dashboard.css', 'text/css')

    def history_css(self) -> ResponseType:
        return self._static('history.css', 'text/css')

    def admin_css(self) -> ResponseType:
        return self._static('admin.css', 'text/css')

    def dashboard_js(self) -> ResponseType:
        return self._static('dashboard.js', 'text/javascript')

    def history_js(self) -> ResponseType:
        return self._static('history.js', 'text/javascript')

    def gui_panel(self) -> ResponseType:
        auth_token = request.cookies.get('auth_token')
        if not self.validate_auth_token(auth_token):
            return make_response(redirect(f"{self.prefix}/auth"))
        return self._page('dashboard.html')
    def gui_auth(self) -> ResponseType:
        return self._page('auth.html')
    def gui_history(self) -> ResponseType:
        auth_token = request.cookies.get('auth_token')
        if not self.validate_auth_token(auth_token):
            return make_response(redirect(f"{self.prefix}/auth"))
        return self._page('history.html')

    def _page(self, name: str) -> Response:
        page = {'auth.html': 'auth', 'dashboard.html': 'dashboard', 'history.html': 'history'}[name]
        lang, strings, fallback = web_lang_tables(self.sub.res.lang_cfg, page)
        html = render_template(
            name,
            prefix=self.prefix,
            asset_version=asset_version(),
            lang=lang,
            L=strings,
            L_en=fallback,
        )
        response = Response(html, mimetype='text/html')
        if request.args.get('lang', '').lower() in ('en', 'ru'):
            response.set_cookie('lang', lang, max_age=31536000, samesite='Lax', path='/')
        return response
    
    @requires_webapi_auth
    def qr(self, username: str) -> ResponseType:
        lang = request.args.get('lang', 'en')
        if lang not in ('en', 'ru'):
            lang = 'en'
        
        token = self.sub.user_svc.get_token(username)
        domain = self.cfg['domain']
        link = f"{domain}/{str(self.cfg['uri']).strip('/')}?token={token}&lang={lang}"
        
        if parse_bool(request.args.get('happ')):
            link = f"happ://add/{link}"
        
        try:
            buf = make_qr(link)
        except ValueError:
            return err("Invalid request", 400)

        response = make_response(send_file(buf, mimetype='image/png'))
        response.headers['Cache-Control'] = 'private, max-age=300'
        return response

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
    def profiles(self, username: str) -> ResponseType:
        lang = request.args.get('lang')
        if lang not in ('ru', 'en'):
            return err("Unknown language", 400)

        index = 0 if lang == 'en' else 1
        obj: dict[str, str] = {}
        for i_name, name in self.cfg['profiles'].items():
            name = name[index]
            desc = self.cfg['profileDescriptions'][i_name][index]
            obj[name] = desc
        return ok(obj=obj)
    @requires_webapi_auth
    def fps(self, username: str) -> ResponseType:
        return ok(obj=self.cfg['fingerprints'])
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
    @requires_webapi_auth
    @requires_fields()
    def settings(self, username: str) -> ResponseType:
        """Update users display name or fingerprint.
        Changing ext_username or ext_password requires `current_password` in body."""
        content = g.json_obj
        displayname: str | None = content.get('name', None)
        fingerprint: str | None = content.get('fingerprint', None)
        ext_username: str | None = content.get('username', None)
        ext_password: str | None = content.get('password', None)
        current_password: str | None = content.get('current_password', None)
        if fingerprint:
            if fingerprint not in self.cfg['fingerprints']:
                return err("Unknown fingerprint")
        if displayname:
            if len(displayname) > 16:
                return err("displayname exceeds max. length of 16")
        if ext_username:
            if len(ext_username) > 32:
                return err("username exceeds max. length of 32")
        # credential changes require the current password (guards stolen cookies)
        if ext_username or ext_password:
            if not current_password:
                return err("current_password required to change credentials", 400)
            cur_ext = self.sub.user_svc.get_external_username(username)
            if not cur_ext:
                return err("Account has no credentials set", 400)
            if self.sub.password_svc.validate_credentials(cur_ext, current_password) != username:
                return err("Invalid current password", 401)
        self.sub.business_svc.update_params(
            username=username,
            ext_username=ext_username,
            ext_password=ext_password,
            displayname=displayname,
            fingerprint=fingerprint
        )
        return ok()
    @requires_webapi_auth
    def reset(self, username: str) -> ResponseType:
        """Reset token and UUID (api wrapper)"""
        x = self.sub.business_svc.reset_user(username)
        resp, code = ok(obj=asdict(x))
        self._clear_auth_cookies(resp)
        return resp, code
    @requires_webapi_auth
    @requires_fields_strict(('code', str))
    def bonus(self, username: str) -> ResponseType:
        """Apply bonus code."""
        content = g.json_obj
        
        result = self.sub.code_svc.apply_bonus_code(
            username=username,
            code=cast(str, content["code"]),
        )
        return ok(obj=asdict(result))
    @requires_webapi_auth
    def stats(self, username: str) -> ResponseType:
        """Get user info from token"""
        x = self.sub.business_svc.get_info(username)
        return ok(obj=asdict(x))

    @requires_webapi_auth
    def bandwidth_history(self, username: str) -> ResponseType:
        """Return bandwidth history snapshots for the authenticated user."""
        try:
            days = int(request.args.get('days', 30))
        except (ValueError, TypeError):
            return err("'days' must be an integer")
        days = max(1, min(days, 90))
        snapshots = self.sub.bandwidth_svc.get_bw_history(username, days)
        return ok(obj=[asdict(s) for s in snapshots])


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