from __future__ import annotations

from dataclasses import asdict
from typing import ClassVar, cast

from flask import g, request

from api.admin._base import AdminApiMixin
from api.common import ResponseType, Route
from api.decorators import requires_admin_auth, requires_args, requires_fields_strict
from errors import PanelUnavailableError
from util import err, ok, parse_bool


class UserRoutes(AdminApiMixin):
    """Admin user and fingerprint routes."""

    ROUTES: ClassVar[tuple[Route, ...]] = (
        Route('GET', '/api/user/list', 'user_list'),
        Route('GET', '/api/user/info', 'user_info'),
        Route('POST', '/api/user/add', 'user_add'),
        Route('POST', '/api/user/delete', 'user_delete'),
        Route('GET', '/api/user/refresh', 'user_refresh'),
        Route('GET', '/api/user/onlines', 'user_onlines'),
        Route('POST', '/api/user/reset', 'user_reset'),
        Route('POST', '/api/user/update', 'user_update'),
        Route('GET', '/api/user/history', 'user_history'),
        Route('GET', '/api/fingerprints', 'fingerprints'),
    )

    @requires_admin_auth
    def user_list(self) -> ResponseType:
        users = self.sub.user_svc.list_users()
        if not users:
            return ok(obj=[])
        return ok(obj=users)

    @requires_admin_auth
    @requires_args('user')
    def user_info(self) -> ResponseType:
        username = request.args['user']
        pretty = request.args.get('beautify', '').lower() in ('1', 'true', 'yes')
        x = self.sub.business_svc.get_info(username=username, pretty=pretty)
        return ok(obj=asdict(x))

    @requires_admin_auth
    @requires_fields_strict(
        ('user', str),
        ('displayname', str)
    )
    def user_add(self) -> ResponseType:
        content = g.json_obj
        raw_data: dict[str, object] = {
            "user": content.get('user'),
            "displayname": content.get('displayname'),
            "ext_username": content.get('ext_username', None),
            "ext_password": content.get('ext_password', None),
            "token": content.get('token', None),
            "userid": content.get('userid', None),
            "fingerprint": content.get('fingerprint', None),
            "limit": content.get('limit', 0),
            "wl_limit": content.get('wl_limit', 5),
            "time": content.get('time', 0),
        }

        data: dict[str, str | int] = {}

        for k, v in raw_data.items():
            if k in ('user', 'displayname'):
                if not isinstance(v, str):
                    return err(f"{k} must be a string")
                data[k] = v

            elif k in ('ext_username', 'ext_password', 'token', 'userid', 'fingerprint'):
                if v is not None and not isinstance(v, str):
                    return err(f"{k} must be a string or null")
                data[k] = cast(str, v)

            elif k in ('limit', 'wl_limit', 'time'):
                try:
                    data[k] = int(cast(str, v))
                except (ValueError, TypeError):
                    return err(f"{k} must be an integer, got: {v}")
                if cast(int, data[k]) < 0:
                    return err(f"{k} must be non-negative")

        self.sub.business_svc.add_new_user(
            username=cast(str, data['user']),
            displayname=cast(str, data['displayname']),
            ext_username=cast(str, data['ext_username']),
            ext_password=cast(str, data['ext_password']),
            token=cast(str, data['token']),
            userid=cast(str, data['userid']),
            fingerprint=cast(str, data['fingerprint']),
            limit=cast(int, data['limit']),
            wl_limit=cast(int, data['wl_limit']),
            timee=cast(int, data['time'])
        )
        return ok("Created", 201)

    @requires_admin_auth
    @requires_fields_strict(('user', str))
    def user_delete(self) -> ResponseType:
        content = g.json_obj
        username: str = content.get('user')
        perma = parse_bool(content.get('perma', 'true'))
        if perma is None:
            return err("'perma' must be bool-like")
        self.sub.business_svc.delete_user(username, perma)
        return ok("Deleted")

    @requires_admin_auth
    def user_refresh(self) -> ResponseType:
        users = self.sub.user_svc.list_users()
        failures: list[str] = []
        known = self.sub.panel_svc.client_maps(self.sub.panels)
        for cc in users:
            try:
                self.sub.business_svc.add_users(cc, known_clients=known)
            except PanelUnavailableError:
                failures.append(cc)
                self.log.exception("user refresh failed for %s", cc)
            except Exception:
                self.log.critical("bulk user refresh aborted for %s", cc, exc_info=True)
                return err(
                    "Refresh aborted",
                    500,
                    {
                        "failed": failures,
                        "aborted": cc,
                        "succeeded": users.index(cc) - len(failures),
                        "total": len(users),
                    },
                )
        if failures and len(failures) == len(users):
            return err(
                "Panel refresh failed for all users",
                502,
                {"failed": failures, "total": len(users)},
            )
        if failures:
            return ok(
                "Refresh completed with panel failures",
                obj={"failed": failures, "succeeded": len(users) - len(failures), "total": len(users)},
            )
        return ok("Refreshed all users.")

    @requires_admin_auth
    def user_onlines(self) -> ResponseType:
        new = parse_bool(request.args.get('keyed', False))
        if new is None:
            return err("'keyed' must be bool-like")
        status = self.sub.panel_svc.get_online_status(new)
        return ok(obj={"users": status.users, "panel_health": status.panel_health})

    @requires_admin_auth
    @requires_fields_strict(('user', str))
    def user_reset(self) -> ResponseType:
        content = g.json_obj
        username: str = content.get('user')
        x = self.sub.business_svc.reset_user(username)
        return ok(obj=asdict(x))

    @requires_admin_auth
    @requires_fields_strict(('user', str))
    def user_update(self) -> ResponseType:
        content = g.json_obj
        username: str = content.get('user')
        fingerprint = content.get('fingerprint')
        displayname = content.get('displayname')
        if fingerprint is not None and not isinstance(fingerprint, str):
            return err("fingerprint must be a string or null")
        if displayname is not None and not isinstance(displayname, str):
            return err("displayname must be a string or null")

        def _optional_int(name: str) -> int | None:
            if name not in content:
                return None
            try:
                value = int(cast(str | int, content.get(name)))
            except (ValueError, TypeError):
                raise ValueError(name) from None
            if value < 0:
                raise ValueError(name)
            return value

        try:
            limit = _optional_int('limit')
            wl_limit = _optional_int('wl_limit')
            timee = _optional_int('time')
        except ValueError as error:
            return err(f"{error.args[0]} must be a non-negative integer")

        self.sub.business_svc.update_params(
            username=username,
            displayname=displayname,
            fingerprint=fingerprint,
            limit=limit,
            wl_limit=wl_limit,
            timee=timee,
        )
        return ok("Updated")

    @requires_admin_auth
    @requires_args('user')
    def user_history(self) -> ResponseType:
        username = request.args['user']
        try:
            days = int(request.args.get('days', 30))
        except (ValueError, TypeError):
            return err("'days' must be an integer")
        days = max(1, min(days, 90))
        snapshots = self.sub.bandwidth_svc.get_bw_history(username, days)
        return ok(obj=[asdict(s) for s in snapshots])

    @requires_admin_auth
    def fingerprints(self) -> ResponseType:
        conf = self.cfg.view()
        return ok(obj=conf['fingerprints'])
