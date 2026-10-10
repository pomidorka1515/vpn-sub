from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, ClassVar, Literal, cast

from flask import g, request

from api.admin._base import AdminApiMixin
from api.common import ResponseType, Route
from api.decorators import requires_admin_auth, requires_args, requires_fields_strict
from util import err, ok, parse_bool

if TYPE_CHECKING:
    from custom_types import JsonifyValue


class CodeRoutes(AdminApiMixin):
    """Admin invite-code routes."""

    ROUTES: ClassVar[tuple[Route, ...]] = (
        Route('GET', '/api/code/list', 'code_list'),
        Route('GET', '/api/code/info', 'code_info'),
        Route('POST', '/api/code/add', 'code_add'),
        Route('POST', '/api/code/delete', 'code_delete'),
    )

    @requires_admin_auth
    def code_list(self) -> ResponseType:
        return ok(obj=self.sub.code_svc.list_code())

    @requires_admin_auth
    @requires_args('code')
    def code_info(self) -> ResponseType:
        code = request.args.get('code')
        if not isinstance(code, str):
            return err("'code' must be a str")
        x = self.sub.code_svc.get_code(code)
        return ok(obj=asdict(x))

    @requires_admin_auth
    @requires_fields_strict(
        ('code', str),
        ('action', str)
    )
    def code_add(self) -> ResponseType:
        content = g.json_obj
        raw_data: dict[
            Literal["name", "action", "permanent", "uses", "days", "gb", "wl_gb"],
            JsonifyValue
        ] = {
            "name": content.get('code'),
            "action": content.get('action'),
            "permanent": content.get('perma', 'false'),
            "uses": content.get('uses', 1),
            "days": content.get('days', 0),
            "gb": content.get('gb', 0),
            "wl_gb": content.get('wl_gb', 0)
        }

        data: dict[str, str | int] = {}

        for k, v in raw_data.items():
            match k:
                case 'permanent':
                    parsed = parse_bool(v)
                    if parsed is None:
                        return err(f"{k} must be boolean-like (true/false, yes/no, 1/0)")
                    data[k] = parsed

                case 'days' | 'gb' | 'wl_gb' | 'uses':
                    try:
                        data[k] = int(cast(str, v))
                    except (ValueError, TypeError):
                        return err(f"{k} must be an integer, got: {v}")

                case 'name' | 'action':
                    if not isinstance(v, str):
                        return err(f"{k} must be a string, got: {v}")
                    data[k] = v

        if data['action'] not in ('register', 'bonus'):
            return err("action must be 'register' or 'bonus'")

        if cast(int, data['days']) < 0 or cast(int, data['gb']) < 0 or cast(int, data['wl_gb']) < 0:
            return err("days, gb, and wl_gb must be non-negative")

        self.sub.code_svc.add_code(
            code=cast(str, data['name']),
            action=data['action'],
            permanent=cast(bool, data['permanent']),
            days=cast(int, data['days']),
            gb=cast(int, data['gb']),
            wl_gb=cast(int, data['wl_gb']),
            uses=cast(int, data['uses'])
        )
        return ok("Created", 201)

    @requires_admin_auth
    @requires_fields_strict(('code', str))
    def code_delete(self) -> ResponseType:
        content = g.json_obj
        name: str = content.get('code')
        self.sub.code_svc.delete_code(name)
        return ok("Deleted")
