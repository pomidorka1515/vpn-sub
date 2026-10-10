from __future__ import annotations

import re
from collections.abc import Callable
from typing import ClassVar

from flask import Response, make_response, redirect, render_template, request, send_file

from api.common import (
    RES_DIR,
    ResponseType,
    Route,
    admin_module_names,
    asset_version,
    web_lang_tables,
)
from api.web._base import WebApiMixin
from fonts import FONT_FILES, embed_font_faces
from util import err


def _admin_module_method(name: str) -> Callable[[WebApiMixin], ResponseType]:
    def handler(self: WebApiMixin) -> ResponseType:
        version = asset_version()
        source = (RES_DIR / 'admin' / f'{name}.js').read_text(encoding='utf-8')
        # Version import specifiers only. A quote-suffix replace missed
        # "./charts/spec.js" because nested modules do not end in .js'.
        body = re.sub(
            r'''(?P<lead>from\s+)(?P<quote>['"])(?P<path>\.{1,2}/[^'"]+\.js)(?P=quote)''',
            lambda match: (
                f"{match.group('lead')}{match.group('quote')}"
                f"{match.group('path')}?v={version}{match.group('quote')}"
            ),
            source,
        )
        return self._static(f'admin/{name}.js', 'text/javascript', body.encode('utf-8')) # pyright: ignore[reportPrivateUsage]
    handler.__name__ = 'admin_' + name.replace('/', '_') + '_js'
    return handler


class StaticRoutes(WebApiMixin):
    """Pages, CSS/JS, fonts, and the redirect interstitial."""

    _admin_module = ''
    for _admin_module in admin_module_names():
        locals()['admin_' + _admin_module.replace('/', '_') + '_js'] = _admin_module_method(_admin_module)
    del _admin_module

    ROUTES: ClassVar[tuple[Route, ...]] = (
        Route('GET', '/redirect', 'redirect_page'),
        Route('GET', '/common.js', 'common_js'),
        Route('GET', '/common.css', 'common_css'),
        Route('GET', '/fonts/<name>', 'font_file'),
        Route('GET', '/auth.css', 'auth_css'),
        Route('GET', '/dashboard.css', 'dashboard_css'),
        Route('GET', '/history.css', 'history_css'),
        Route('GET', '/charts.css', 'charts_css'),
        Route('GET', '/admin.css', 'admin_css'),
        *(Route('GET', f'/admin/{name}.js', 'admin_' + name.replace('/', '_') + '_js') for name in admin_module_names()),
        Route('GET', '/dashboard.js', 'dashboard_js'),
        Route('GET', '/history.js', 'history_js'),
        Route('GET', '/chart.umd.min.js', 'chart_js'),
        Route('GET', '/panel', 'gui_panel'),
        Route('GET', '/auth', 'gui_auth'),
        Route('GET', '/history', 'gui_history'),
    )

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
        source = (RES_DIR / 'common.css').read_text(encoding='utf-8')
        body = embed_font_faces(source, self.prefix)
        return self._static('common.css', 'text/css', body.encode('utf-8'))

    def font_file(self, name: str) -> ResponseType:
        if name not in FONT_FILES:
            return err('Not found', 404)
        # send_file goes through gunicorn sendfile(), which seeks the payload
        # file back. Nuitka onefile then drops it, and the body is empty.
        body = (RES_DIR / 'fonts' / name).read_bytes()
        return self._static(f'fonts/{name}', 'font/woff2', body)

    def auth_css(self) -> ResponseType:
        return self._static('auth.css', 'text/css')

    def dashboard_css(self) -> ResponseType:
        return self._static('dashboard.css', 'text/css')

    def history_css(self) -> ResponseType:
        return self._static('history.css', 'text/css')

    def charts_css(self) -> ResponseType:
        return self._static('charts.css', 'text/css')

    def admin_css(self) -> ResponseType:
        return self._static('admin.css', 'text/css')

    def dashboard_js(self) -> ResponseType:
        return self._static('dashboard.js', 'text/javascript')

    def history_js(self) -> ResponseType:
        return self._static('history.js', 'text/javascript')

    def chart_js(self) -> ResponseType:
        return self._static('vendor/chart.umd.min.js', 'text/javascript')

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
