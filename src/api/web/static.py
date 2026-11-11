from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from quart import Response, make_response, redirect, render_template, request, send_file  # pyright: ignore[reportUnknownVariableType]  # Upstream PathLike lacks a type argument.

from api.common import (
    RES_DIR,
    Route,
    asset_version,
    web_lang_tables,
)
from api.web._base import AsyncWebApiMixin
from fonts import FONT_FILES, embed_font_faces
from util import err

if TYPE_CHECKING:
    from api.common import (
        ResponseType,
    )


class AsyncStaticRoutes(AsyncWebApiMixin):
    """Pages, CSS/JS, fonts, and the redirect interstitial."""

    ROUTES: ClassVar[tuple[Route, ...]] = (
        Route("GET", "/redirect", "redirect_page"),
        Route("GET", "/common.js", "common_js"),
        Route("GET", "/common.css", "common_css"),
        Route("GET", "/fonts/<name>", "font_file"),
        Route("GET", "/auth.css", "auth_css"),
        Route("GET", "/auth.js", "auth_js"),
        Route("GET", "/admin-auth.js", "admin_auth_js"),
        Route("GET", "/error.js", "error_js"),
        Route("GET", "/redirect.js", "redirect_js"),
        Route("GET", "/dashboard.css", "dashboard_css"),
        Route("GET", "/history.css", "history_css"),
        Route("GET", "/dashboard.js", "dashboard_js"),
        Route("GET", "/history.js", "history_js"),
        Route("GET", "/chart.umd.min.js", "chart_js"),
        Route("GET", "/panel", "gui_panel"),
        Route("GET", "/auth", "gui_auth"),
        Route("GET", "/reset", "gui_password_reset"),
        Route("GET", "/reset.js", "password_reset_js"),
        Route("GET", "/history", "gui_history"),
    )

    def reg_handles(self) -> None:
        self.app.after_request(self._session_assets_no_store)

    async def _session_assets_no_store(self, response: Response) -> Response:
        # Include rejected requests and authentication/storage failures too.
        if request.endpoint in {
            "dashboard_js",
            "history_js",
            "chart_js",
            "dashboard_css",
            "history_css",
        }:
            response.headers["Cache-Control"] = "no-store"
        return response

    async def _session_asset(self, name: str, mimetype: str) -> ResponseType:
        if not await self.validate_auth_token(request.cookies.get("auth_token")):
            return err("Invalid auth token.", 401)
        return await send_file(
            RES_DIR / name,
            mimetype=mimetype,
            add_etags=False,
            cache_timeout=0,
        )

    async def redirect_page(self) -> ResponseType:
        prefix = request.args.get("prefix", "")
        if not prefix.startswith(("happ://", "v2ray", "clash")):
            return err("Invalid prefix", 400)
        if len(prefix) > 512:
            return err("Prefix too long", 400)
        return await make_response(
            await send_file(RES_DIR / "templates" / "redirect.html", add_etags=False),
        )

    async def _static(self, name: str, mimetype: str, body: bytes | None = None) -> ResponseType:
        # url is versioned via ?v= (asset_version), so long caching is safe
        if body is None:
            response = await make_response(
                await send_file(
                    RES_DIR
                    / (name if name.startswith(("fonts/", "vendor/")) else f"assets/{name}"),
                    mimetype=mimetype,
                    add_etags=True,
                    cache_timeout=31536000,
                ),
            )
        else:
            response = await make_response(body)
            response.mimetype = mimetype
            response.cache_control.max_age = 31536000
            response.cache_control.public = True
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    async def common_js(self) -> ResponseType:
        return await self._static("common.js", "text/javascript")

    async def common_css(self) -> ResponseType:
        body = await self.sub.res.offloads.files.run(
            lambda: embed_font_faces(
                (RES_DIR / "assets" / "common.css").read_text(encoding="utf-8"),
                self.prefix,
            ).encode("utf-8"),
        )
        return await self._static("common.css", "text/css", body)

    async def font_file(self, name: str) -> ResponseType:
        if name not in FONT_FILES:
            return err("Not found", 404)
        # Buffer bundled fonts before serving so onefile payload lifetime
        # cannot invalidate a deferred file response.
        body = await self.sub.res.offloads.files.run((RES_DIR / "fonts" / name).read_bytes)
        return await self._static(f"fonts/{name}", "font/woff2", body)

    async def auth_css(self) -> ResponseType:
        return await self._static("auth.css", "text/css")

    async def auth_js(self) -> ResponseType:
        return await self._static("auth.js", "text/javascript")

    async def admin_auth_js(self) -> ResponseType:
        return await self._static("admin-auth.js", "text/javascript")

    async def error_js(self) -> ResponseType:
        return await self._static("error.js", "text/javascript")

    async def redirect_js(self) -> ResponseType:
        # redirect.html references this script without a version query.
        response = await make_response(
            await send_file(
                RES_DIR / "assets" / "redirect.js", mimetype="text/javascript", conditional=True
            ),
        )
        response.headers["Cache-Control"] = "no-cache"
        return response

    async def dashboard_css(self) -> ResponseType:
        return await self._session_asset("assets/dashboard.css", "text/css")

    async def history_css(self) -> ResponseType:
        return await self._session_asset("assets/history.css", "text/css")

    async def dashboard_js(self) -> ResponseType:
        return await self._session_asset("assets/dashboard.js", "text/javascript")

    async def history_js(self) -> ResponseType:
        return await self._session_asset("assets/history.js", "text/javascript")

    async def chart_js(self) -> ResponseType:
        return await self._session_asset("vendor/chart.umd.min.js", "text/javascript")

    async def gui_panel(self) -> ResponseType:
        auth_token = request.cookies.get("auth_token")
        if not await self.validate_auth_token(auth_token):
            return await make_response(redirect(f"{self.prefix}/auth"))
        return await self._page("dashboard.html")

    async def gui_auth(self) -> ResponseType:
        return await self._page("auth.html")

    async def password_reset_js(self) -> ResponseType:
        return await self._static("reset.js", "text/javascript")

    async def gui_password_reset(self) -> ResponseType:
        await self.sub.account_svc.password_reset(request.args.get("token"))
        response = await self._page("reset.html")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    async def gui_history(self) -> ResponseType:
        auth_token = request.cookies.get("auth_token")
        if not await self.validate_auth_token(auth_token):
            return await make_response(redirect(f"{self.prefix}/auth"))
        return await self._page("history.html")

    async def _page(self, name: str) -> Response:
        page = {
            "auth.html": "auth",
            "reset.html": "auth",
            "dashboard.html": "dashboard",
            "history.html": "history",
        }[name]
        lang, strings, fallback = web_lang_tables(self.sub.res.lang_cfg, page)
        html = await render_template(
            name,
            prefix=self.prefix,
            asset_version=await self.sub.res.offloads.files.run(asset_version),
            lang=lang,
            L=strings,
            L_en=fallback,
            L_ru=web_lang_tables(self.sub.res.lang_cfg, page, "ru")[1],
        )
        response = Response(html, mimetype="text/html")
        if request.args.get("lang", "").lower() in {"en", "ru"}:
            response.set_cookie("lang", lang, max_age=31536000, samesite="Lax", path="/")
        return response
