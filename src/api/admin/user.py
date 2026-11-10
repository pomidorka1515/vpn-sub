from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, ClassVar, cast

from quart import g, request

from api.admin._base import AsyncAdminApiMixin
from api.admin._numeric import MAX_GB, SQLITE_MAX_INT, bounded_int
from api.common import Route
from api.decorators import requires_admin_auth, requires_args, requires_fields_strict
from custom_types import UserUpdateFields
from util import err, ok, parse_bool

if TYPE_CHECKING:
    from api.common import ResponseType


class AsyncUserRoutes(AsyncAdminApiMixin):
    """Admin user and fingerprint routes."""

    ROUTES: ClassVar[tuple[Route, ...]] = (
        Route("GET", "/api/user/list", "user_list"),
        Route("GET", "/api/user/info", "user_info"),
        Route("POST", "/api/user/add", "user_add"),
        Route("POST", "/api/user/delete", "user_delete"),
        Route("POST", "/api/user/refresh", "user_refresh"),
        Route("GET", "/api/user/onlines", "user_onlines"),
        Route("POST", "/api/user/reset", "user_reset"),
        Route("POST", "/api/user/password-reset", "user_password_reset"),
        Route("POST", "/api/user/update", "user_update"),
        Route("GET", "/api/user/history", "user_history"),
        Route("GET", "/api/fingerprints", "fingerprints"),
    )

    @requires_admin_auth
    @requires_fields_strict(("user", str))
    async def user_password_reset(self) -> ResponseType:
        link = await self.sub.account_svc.issue_password_reset(g.json_obj["user"])
        response, status = ok(obj={"link": link})
        response.headers["Cache-Control"] = "no-store"
        return response, status

    @requires_admin_auth
    async def user_list(self) -> ResponseType:
        if request.args.get("include_external") == "1":
            return ok(
                obj=[
                    {"username": user["username"], "ext_username": user["ext_username"]}
                    for user in await self.sub.user_svc.list_user_states()
                ]
            )
        users = await self.sub.user_svc.list_users()
        if not users:
            return ok(obj=[])
        return ok(obj=users)

    @requires_admin_auth
    @requires_args("user")
    async def user_info(self) -> ResponseType:
        username = request.args["user"]
        pretty = request.args.get("beautify", "").lower() in {"1", "true", "yes"}
        x = await self.sub.business_svc.get_info(username=username, pretty=pretty)
        info = asdict(x)
        info["time_exact"] = str(x.time)
        info["ext_username"] = await self.sub.user_svc.get_external_username(username)
        return ok(obj=info)

    @requires_admin_auth
    @requires_fields_strict(("user", str), ("displayname", str))
    async def user_add(self) -> ResponseType:
        content = g.json_obj
        raw_data: dict[str, object] = {
            "user": content.get("user"),
            "displayname": content.get("displayname"),
            "ext_username": content.get("ext_username", None),
            "ext_password": content.get("ext_password", None),
            "token": content.get("token", None),
            "userid": content.get("userid", None),
            "fingerprint": content.get("fingerprint", None),
            "limit": content.get("limit", 0),
            "wl_limit": content.get("wl_limit", 5),
            "time": content.get("time", 0),
        }

        data: dict[str, str | int] = {}

        for k, v in raw_data.items():
            if k in {"user", "displayname"}:
                if not isinstance(v, str):
                    return err(f"{k} must be a string")
                data[k] = v

            elif k in {"ext_username", "ext_password", "token", "userid", "fingerprint"}:
                if v is not None and not isinstance(v, str):
                    return err(f"{k} must be a string or null")
                data[k] = cast(str, v)

            elif k in {"limit", "wl_limit", "time"}:
                try:
                    data[k] = bounded_int(k, v, maximum=SQLITE_MAX_INT if k == "time" else MAX_GB)
                except ValueError as error:
                    return err(str(error), 400)

        await self.sub.business_svc.add_new_user(
            username=cast(str, data["user"]),
            displayname=cast(str, data["displayname"]),
            ext_username=cast(str, data["ext_username"]),
            ext_password=cast(str, data["ext_password"]),
            token=cast(str, data["token"]),
            userid=cast(str, data["userid"]),
            fingerprint=cast(str, data["fingerprint"]),
            limit=cast(int, data["limit"]),
            wl_limit=cast(int, data["wl_limit"]),
            timee=cast(int, data["time"]),
        )
        return ok("Created", 201)

    @requires_admin_auth
    @requires_fields_strict(("user", str))
    async def user_delete(self) -> ResponseType:
        content = g.json_obj
        username: str = content.get("user")
        if "perma" in content:
            return err("'perma' is no longer supported; deletion is always permanent")
        await self.sub.business_svc.delete_user(username)
        return ok("Deleted")

    @requires_admin_auth
    async def user_refresh(self) -> ResponseType:
        result = await self.sub.refresh_svc.refresh()
        failures = result.failed
        aborted = result.aborted
        succeeded = result.succeeded
        if aborted is not None:
            return err(
                "Refresh aborted",
                500,
                {
                    "failed": failures,
                    "aborted": aborted,
                    "succeeded": succeeded,
                    "total": result.total,
                },
            )
        if failures and len(failures) == result.total:
            return err(
                "Panel refresh failed for all users",
                502,
                {"failed": failures, "total": result.total},
            )
        if failures:
            return ok(
                "Refresh completed with panel failures",
                obj={
                    "failed": failures,
                    "succeeded": succeeded,
                    "total": result.total,
                },
            )
        return ok("Refreshed all users.")

    @requires_admin_auth
    async def user_onlines(self) -> ResponseType:
        new = parse_bool(request.args.get("keyed", False))
        if new is None:
            return err("'keyed' must be bool-like")
        status = await self.sub.panel_svc.get_online_status(new)
        return ok(obj={"users": status.users, "panel_health": status.panel_health})

    @requires_admin_auth
    @requires_fields_strict(("user", str))
    async def user_reset(self) -> ResponseType:
        content = g.json_obj
        username: str = content.get("user")
        x = await self.sub.business_svc.reset_user(username)
        return ok(obj=asdict(x))

    @requires_admin_auth
    @requires_fields_strict(("username", str))
    async def user_update(self) -> ResponseType:
        content = g.json_obj
        allowed = {
            "username",
            "new_username",
            "displayname",
            "fingerprint",
            "time",
            "limit",
            "used",
            "wl_limit",
            "wl_used",
        }
        unknown = content.keys() - allowed
        if unknown:
            return err(f"Unsupported fields: {', '.join(sorted(unknown))}")
        values: dict[str, str | int] = {}
        for field in ("new_username", "displayname", "fingerprint"):
            if field in content:
                if not isinstance(content[field], str):
                    return err(f"{field} must be a string")
                values[field] = content[field]
        try:
            for field, maximum in (
                ("time", SQLITE_MAX_INT),
                ("limit", MAX_GB),
                ("wl_limit", MAX_GB),
                ("used", SQLITE_MAX_INT),
                ("wl_used", SQLITE_MAX_INT),
            ):
                if field in content:
                    values["timee" if field == "time" else field] = bounded_int(
                        field, content[field], maximum=maximum
                    )
        except ValueError as error:
            return err(str(error), 400)
        updated = await self.sub.business_svc.edit_user(
            username=content["username"], **cast(UserUpdateFields, values)
        )
        return ok("Updated", obj={"updated": updated})

    @requires_admin_auth
    @requires_args("user")
    async def user_history(self) -> ResponseType:
        username = request.args["user"]
        try:
            days = int(request.args.get("days", 30))
        except ValueError, TypeError:
            return err("'days' must be an integer")
        days = max(1, min(days, 90))
        snapshots = await self.sub.bandwidth_svc.get_bw_history(username, days)
        return ok(obj=[asdict(s) for s in snapshots])

    @requires_admin_auth
    async def fingerprints(self) -> ResponseType:
        conf = self.cfg.view()
        return ok(obj=conf["fingerprints"])
