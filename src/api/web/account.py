from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, ClassVar, cast

from quart import g, make_response, request, send_file  # pyright: ignore[reportUnknownVariableType]  # Upstream PathLike lacks a type argument.

from api.common import Route
from api.decorators import requires_fields, requires_fields_strict, requires_webapi_auth
from api.web._base import AsyncWebApiMixin
from util import err, make_qr, ok, parse_bool

if TYPE_CHECKING:
    from api.common import ResponseType


class AsyncAccountRoutes(AsyncWebApiMixin):
    """Authenticated account, stats, and subscription helpers."""

    ROUTES: ClassVar[tuple[Route, ...]] = (
        Route("POST", "/webapi/bonus", "bonus", 15),
        Route("GET", "/webapi/stats", "stats", 20),
        Route("POST", "/webapi/reset", "reset", 3),
        Route("POST", "/webapi/settings", "settings", 15),
        Route("GET", "/webapi/fingerprints", "fps", None),
        Route("GET", "/webapi/profiles", "profiles", 60),
        Route("GET", "/webapi/history", "bandwidth_history", 30),
        Route("GET", "/webapi/qr", "qr", 80),
    )

    @requires_webapi_auth
    async def qr(self, username: str) -> ResponseType:
        lang = request.args.get("lang", "en")
        if lang not in {"en", "ru"}:
            lang = "en"

        token = await self.sub.user_svc.get_token(username)
        conf = self.cfg.view()
        domain = conf["domain"]
        link = f"{domain}/{conf['uri'].strip('/')}?token={token}&lang={lang}"

        if parse_bool(request.args.get("happ")):
            link = f"happ://add/{link}"

        try:
            buf = await self.sub.res.offloads.rendering.run(make_qr, link)
        except ValueError:
            return err("Invalid request", 400)

        response = await make_response(await send_file(buf, mimetype="image/png"))
        response.headers["Cache-Control"] = "private, max-age=300"
        return response

    @requires_webapi_auth
    async def profiles(self, username: str) -> ResponseType:
        del username
        lang = request.args.get("lang")
        if lang not in {"ru", "en"}:
            return err("Unknown language", 400)

        index = 0 if lang == "en" else 1
        obj: dict[str, str] = {}
        conf = self.cfg.view()
        for profile in conf["profiles"].values():
            name = profile["name"][index]
            desc = profile["description"][index]
            obj[name] = desc
        return ok(obj=obj)

    @requires_webapi_auth
    async def fps(self, username: str) -> ResponseType:
        del username
        conf = self.cfg.view()
        return ok(obj=conf["fingerprints"])

    @requires_webapi_auth
    @requires_fields()
    async def settings(self, username: str) -> ResponseType:
        """Update users display name or fingerprint.
        Changing ext_username or ext_password requires `current_password` in body."""
        content = g.json_obj
        displayname: str | None = content.get("name", None)
        fingerprint: str | None = content.get("fingerprint", None)
        ext_username: str | None = content.get("username", None)
        ext_password: str | None = content.get("password", None)
        current_password: str | None = content.get("current_password", None)
        await self.sub.account_svc.settings(
            username=username,
            ext_username=ext_username,
            ext_password=ext_password,
            displayname=displayname,
            fingerprint=fingerprint,
            current_password=current_password,
        )
        return ok()

    @requires_webapi_auth
    async def reset(self, username: str) -> ResponseType:
        """Reset token and UUID (api wrapper)."""
        x = await self.sub.business_svc.reset_user(username)
        resp, code = ok(obj=asdict(x))
        self._clear_auth_cookies(resp)
        return resp, code

    @requires_webapi_auth
    @requires_fields_strict(("code", str))
    async def bonus(self, username: str) -> ResponseType:
        """Apply bonus code."""
        content = g.json_obj

        result = await self.sub.code_svc.apply_bonus_code(
            username=username,
            code=cast("str", content["code"]),
        )
        return ok(obj=asdict(result))

    @requires_webapi_auth
    async def stats(self, username: str) -> ResponseType:
        """Get user info from token."""
        x = await self.sub.business_svc.get_info(username)
        return ok(obj=asdict(x))

    @requires_webapi_auth
    async def bandwidth_history(self, username: str) -> ResponseType:
        """Return bandwidth history snapshots for the authenticated user."""
        try:
            days = int(request.args.get("days", 30))
        except ValueError, TypeError:
            return err("'days' must be an integer")
        days = max(1, min(days, 90))
        snapshots = await self.sub.bandwidth_svc.get_bw_history(username, days)
        return ok(obj=[asdict(s) for s in snapshots])
