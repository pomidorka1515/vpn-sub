from .common import BaseApi, Route, ResponseType, RES_DIR
from .decorators import requires_args, requires_fields_strict, requires_basic_admin_auth, requires_admin_auth
from flask import Flask, g, request, send_file
from custom_types import JsonifyValue
from util import ok, err, parse_bool, SysUtil
from dataclasses import asdict
from config import ConfigLike, LinesConfigLike
from core import Subscription
from bwatch import BWatch
from loggers import Logger
from typing import cast, Literal
from errors import PanelUnavailableError
from random import random

__all__ = ["Api"]

class Api(BaseApi):
    """Private admin API."""
    ROUTES = [
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

        Route('GET', '/api/panel/status', 'panel_status'),

        Route('GET', '/api/code/list', 'code_list'),
        Route('GET', '/api/code/info', 'code_info'),
        Route('POST', '/api/code/add', 'code_add'),
        Route('POST', '/api/code/delete', 'code_delete'),

        Route('POST', '/api/leaderboard', 'leaderboard'),

        Route('POST', '/api/state/snapshots', 'snapshots'),
        Route('GET', '/api/state/all', 'full_info'),
        Route('GET', '/api/state/system', 'system_status'),
        
        Route('GET', '/api/logs/audit', 'audit'),

        Route('GET', '/api/ui', 'admin_ui'),
        
        Route('GET', '/api/health', 'health'),
        Route('GET', '/api/operations/status', 'operation_status'),
        Route('POST', '/api/operations/rollback/resolve', 'operation_rollback_resolve'),
        Route('GET', '/api/teapot', 'teapot')
    ]

    def __init__(self,
                 app: Flask,
                 cfg: ConfigLike,
                 audit_cfg: LinesConfigLike,
                 sub: Subscription,
                 bw: BWatch):
        self.log = Logger(type(self).__name__)
        uri = '/' + '/'.join(p.strip('/ ') for p in (cfg['uri'], cfg['api_uri']) if p and p.strip('/ '))
        self.token = cfg['api_token']
        self.audit_cfg = audit_cfg
        super().__init__(app, cfg, sub, bw, uri)

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
        for cc in users:
            try:
                self.sub.business_svc.add_users(cc)
            except PanelUnavailableError:
                failures.append(cc)
                self.log.error("user refresh failed for %s", cc, exc_info=True)
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
                raise ValueError(name)
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
        return ok(obj=self.cfg['fingerprints'])
            
    @requires_admin_auth
    def panel_status(self) -> ResponseType: 
        query = request.args.get('name', None)
        if query is None:
            result: dict[str, dict[str, JsonifyValue] | None] = {}
            for panel in self.sub.panels:
                _ = self.sub.panel_svc.getstatus(panel)
                if _ is None:
                    self.log.error(f"getstatus: {panel.name} returned None")
                else:
                    _ = asdict(_)
                result[panel.name] = _ if _ is not None else {"status": "unknown"}
            return ok(obj=result)

        for panel in self.sub.panels:
            if panel.name == query:
                res = self.sub.panel_svc.getstatus(panel)
                if res is None:
                    return err("getstatus() returned None; panel may be down")
                return ok(obj=asdict(res))

        return err("panel not found", 404)
    
    @requires_admin_auth
    def system_status(self) -> ResponseType:
        return ok(obj=asdict(SysUtil.full_info()))
    
    @requires_admin_auth
    def full_info(self) -> ResponseType:
        panels_data: dict[str, JsonifyValue] = {}
        for panel in self.sub.panels:
            res = self.sub.panel_svc.getstatus(panel)
            if res is None:
                panels_data[panel.name] = {"status": "unknown"}
            else:
                panels_data[panel.name] = asdict(res)

        obj: dict[str, JsonifyValue] = {
            "host": asdict(SysUtil.full_info()),
            "panels": panels_data
        }
        return ok(obj=obj)
        
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

    @requires_admin_auth
    def audit(self) -> ResponseType:
        n = request.args.get('n', 50, type=int)
    
        if n < 0:
            return err("'n' arg must be a positive integer, or 0 for the whole file")
    
        if n == 0:
            return ok(obj=self.audit_cfg.read_all())
    
        result = list(self.audit_cfg.tail(n))
        return ok(obj=result)
    
    @requires_admin_auth
    @requires_fields_strict(('cutoff', int))
    def snapshots(self) -> ResponseType:
        content = g.json_obj

        cutoff: int = content.get('cutoff')

        if cutoff < 0:
            return err("cutoff param must be higher than 0", 400)
        
        obj = self.sub.bandwidth_svc.get_snapshots(cutoff)
        return ok(obj=[asdict(s) for s in obj])
    
    @requires_admin_auth
    @requires_fields_strict(
        ('type', str), 
        ('cutoff', int), 
        ('displaynames', bool), 
        ('flip', bool)
    )
    def leaderboard(self) -> ResponseType:
        content = g.json_obj

        category: Literal['total', 'monthly', 'wl_monthly'] = content.get('type')
        top_n: int = content.get('cutoff')
        use_displaynames: bool = content.get('displaynames')
        flip: bool = content.get('flip')

        if category not in ('total', 'monthly', 'wl_monthly'):
            return err(msg="category field must be either 'total', 'monthly' or 'wl_monthly'")

        data = self.sub.leaderboard_svc.leaderboard(
            category=category,
            top_n=top_n,
            use_displaynames=use_displaynames,
            flip=flip
        )
        return ok(obj=[
            {"place": i, "username": user, "amount": amount}
            for i, (user, amount) in enumerate(data.items(), start=1)
        ])
    
    @requires_basic_admin_auth
    def admin_ui(self) -> ResponseType:
        return send_file(RES_DIR / 'admin.html', etag=False)
    
    def health(self) -> ResponseType:
        return ok()

    @requires_admin_auth
    def operation_status(self) -> ResponseType:
        return ok(obj={
            "daily_snapshot_failure": self.bw.get_daily_snapshot_failure(),
            "rollback_failures": self.sub.business_code_svc.get_rollback_failures(),
        })

    @requires_admin_auth
    @requires_fields_strict(('kind', str), ('user', str))
    def operation_rollback_resolve(self) -> ResponseType:
        content = g.json_obj
        kind_value: str = content.get('kind')
        username: str = content.get('user')
        if kind_value == 'uuid':
            self.sub.business_code_svc.clear_rollback_failure('uuid', username)
        elif kind_value == 'registration':
            self.sub.business_code_svc.clear_rollback_failure('registration', username)
        else:
            return err("kind must be 'uuid' or 'registration'")
        return ok("Resolved")

    def teapot(self) -> ResponseType:
        return err("I'm a teapot", 418, obj={"teapot": True if random() < 0.01 else False }) # is it really an error?
