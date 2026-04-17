from __future__ import annotations

from core import Subscription, BWatch, nginx_404
from config import Config
from loggers import Logger

import threading
import time
import uuid
import random
import hmac

from functools import wraps
from flask import Flask, Response, jsonify, send_file, redirect, request, make_response
from abc import ABC, abstractmethod
from typing import cast, Tuple, Any, Callable, Literal, Optional, NamedTuple

JsonifyValue =  str | int | float | bool | dict | list | tuple | uuid.UUID | None
HTTPMethod = Literal['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS']
ResponseType = Tuple[Response, int] | Response

class BaseApi(ABC):
    """Base class for API handlers. Enforces required attributes and route registration."""
    
    ROUTES: list[Route]  # subclasses must define this
    
    def __init__(self,
                 app: Flask, 
                 cfg: Config, 
                 sub: Subscription, 
                 bw: BWatch,
                 uri: str):
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.app = app
            self.cfg = cfg
            self.sub = sub
            self.bw = bw
            self.uri = uri
            self._rl_data: dict = {}
            self._rl_lock = threading.Lock()
            self._register_routes()
            self.reg_handles()

    def __init_subclass__(cls, **kwargs):
        """Called when a class inherits from BaseApi. Validates at import time."""
        super().__init_subclass__(**kwargs)
        
        if not hasattr(cls, 'ROUTES'):
            raise TypeError(f"{cls.__name__} must define ROUTES")
        
        for route in cls.ROUTES:
            route.validate()
            if not hasattr(cls, route.handler):
                raise TypeError(
                    f"{cls.__name__}.ROUTES references '{route.handler}' "
                    f"but no such method exists"
                )
    
    def _register_routes(self):
        for route in self.ROUTES:
            route.register(self)
    
    def reg_handles(self) -> None:
        """Optional: subclass setup beyond route registration (error handlers, etc)."""
        pass

class Route(NamedTuple):
    method: HTTPMethod
    path: str
    handler: str
    rate_limit: Optional[int]

    def validate(self):
        if not self.path.startswith('/'):
            raise ValueError(f"Route path must start with '/', got '{self.path}'")
        if self.rate_limit is not None and self.rate_limit <= 0:
            raise ValueError(f"rate_limit must be positive, got {self.rate_limit}")

    def register(self, api: BaseApi):
        try:
            func = getattr(type(api), self.handler)
        except AttributeError:
            api.log.critical(f"method {self.handler} doesnt exist!")
            return
        if self.rate_limit is not None:
            func = rate_limit(self.rate_limit)(func)
        func = func.__get__(api, type(api))
        api.app.add_url_rule(api.uri + self.path, self.handler, func, methods=[self.method])


def _ok(
    msg: str | None = None,
    code: int = 200,
    obj: JsonifyValue = None
) -> Tuple[Response, int]:
    """Internal helper function to return a successful Response."""
    return jsonify({"success": True, "msg": msg, "obj": obj}), code

def _err(
    msg: str | None = None,
    code: int = 400,
    obj: JsonifyValue = None
) -> Tuple[Response, int]:
    """Internal helper function to return an error Response."""
    return jsonify({"success": False, "msg": msg, "obj": obj}), code

def requires_admin_auth(f):
    """Admin API auth via Authorization header. Returns fake nginx 404 on failure."""
    @wraps(f)
    def wrapper(self, *args, **kwargs):
        if request.headers.get('Authorization') != self.token:
            return self.resp
        return f(self, *args, **kwargs)
    return wrapper


def requires_webapi_auth(f: Callable[..., Any]) -> Callable[..., Any]:
    """WebApi auth via token cookie. Injects `username` as first arg after self.
    Returns 401 on failure."""
    @wraps(f)
    def wrapper(self, *args, **kwargs):
        token = request.cookies.get('token')
        username = self.validate_token(token)
        if not username:
            return _err("Invalid token.", 401)
        return f(self, username, *args, **kwargs)
    return wrapper


def requires_no_auth(f: Callable[..., Any]) -> Callable[..., Any]:
    """WebApi: reject if already authenticated (for register). Returns 403."""
    @wraps(f)
    def wrapper(self, *args, **kwargs):
        token = request.cookies.get('token')
        if token and self.validate_token(token):
            return _err("Must not be authorized.", 403)
        return f(self, *args, **kwargs)
    return wrapper


def rate_limit(max_requests: int) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Rate-limit by IP. Reads _rl_data and _rl_lock from the instance (self)."""
    def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(f)
        def wrapper(self, *args, **kwargs):
            ip = request.headers.get('X-Real-IP', request.remote_addr)
            now = time.time()
            with self._rl_lock:
                stale = [k for k, v in self._rl_data.items() if v and now - v[-1] > 60]
                for k in stale:
                    del self._rl_data[k]
                self._rl_data.setdefault(ip, [])
                self._rl_data[ip] = [t for t in self._rl_data[ip] if now - t < 60]
                if len(self._rl_data[ip]) >= max_requests:
                    return _err(msg="Too many requests.", code=429)
                self._rl_data[ip].append(now)
            return f(self, *args, **kwargs)
        return wrapper
    return decorator


def requires_fields(*fields) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Validate request.json has all named fields. Returns 400 on failure."""
    def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(f)
        def wrapper(*args, **kwargs):
            content = request.json
            if content is None:
                return _err("Missing JSON data.", 400)
            missing = [x for x in fields if x not in content]
            if missing:
                return _err(f"Missing fields: {', '.join(missing)}", 400)
            return f(*args, **kwargs)
        return wrapper
    return decorator


class WebApi(BaseApi):
    """Public api for web ui.
    Dependencies: Subscription, BWatch
    Classes depending on this: none"""
    ROUTES: list[Route] = [
        Route('POST', '/webapi/register', 'register', 5),
        Route('POST', '/webapi/login', 'login', 10),
        Route('POST', '/webapi/bonus', 'bonus', 15),
        Route('GET', '/webapi/stats', 'stats', 20),
        Route('POST', '/webapi/reset', 'reset', 3),
        Route('POST', '/webapi/settings', 'settings', 15),
        Route('POST', '/webapi/logout', 'logout', 20),
        Route('GET', '/webapi/fingerprints', 'fps', None),
        Route('DELETE', '/webapi/delete', 'delete', 3),
        Route('POST', '/webapi/validate', 'validate_username', 75),
        Route('POST', '/webapi/profiles', 'profiles', None),
        Route('GET', '/panel', 'gui_panel', None),
        Route('GET', '/auth', 'gui_auth', None)
    ]
    def __init__(self,
                 app: Flask,
                 cfg: Config,
                 sub: Subscription,
                 bw: BWatch):
        self.log = Logger(type(self).__name__)
        uri = f"/{cfg['uri']}" 
        super().__init__(app, cfg, sub, bw, uri)
    
    def validate_token(self, token: str | None = None) -> str | None:
        def _v(t: str | None) -> str | None:
            if not t or len(t) < 30:
                return None
            x = self.sub.usertotoken(t)
            if not isinstance(x, str):
                return None
            return x
        if token is not None:
            return _v(token)
        else:
            return _v(request.cookies.get('token'))
    def validate_credentials(self, username: str, password: str) -> str | bool:
        """Validate username+password against config. Returns internal username on success."""
        users_pw = cast(dict, self.cfg.get('webui_passwords', {}))
        if username not in users_pw:
            return False
        if not hmac.compare_digest(users_pw[username], self.sub.hash(password)):
            return False
        webui_users = cast(dict, self.cfg.get('webui_users', {}))
        internal = webui_users.get(username)
        if not internal or internal not in self.cfg['users']:
            return False
        return internal
    def gui_panel(self) -> ResponseType:
        token = request.cookies.get('token')
        if not self.validate_token(token):
            return make_response(redirect('/sub/auth'))
        return send_file('/var/www/sub/new/res/dashboard.html', etag=False)
    def gui_auth(self) -> ResponseType:
        return send_file('/var/www/sub/new/res/auth.html', etag=False)
    @requires_fields('username')
    def validate_username(self) -> ResponseType:
        """Check if a username is valid (no illegal chars). POST {"username": "..."}"""
        
        content = cast(dict, request.json)
        raw = cast(str, content.get('username'))
        if not isinstance(raw, str):
            return _err("username must be a string")
        sanitized = self.sub.sanitize(raw)
        valid = raw == sanitized and len(raw) > 0
        taken = sanitized in cast(dict, self.cfg.get('webui_users', {}))
        return _ok(obj={
            "MAX_LENGTH": 32,
            "valid": valid,
            "taken": taken,
            "sanitized": sanitized
        })
    @requires_webapi_auth
    @requires_fields('lang')
    def profiles(self, username) -> ResponseType:
        content = cast(dict, request.json)
        if content['lang'] not in ['ru', 'en']:
            return _err("Unknown language", 400)

        index = 0 if content['lang'] == 'en' else 1
        obj = {}
        for i_name, name in self.cfg['profiles'].items():
            name = name[index]
            desc = self.cfg['profileDescriptions'][i_name][index]
            obj[name] = desc
        return _ok(obj=obj)
    @requires_webapi_auth
    def fps(self, username) -> ResponseType:
        return _ok(obj=self.cfg['fingerprints'])
    @requires_webapi_auth
    def delete(self, username) -> ResponseType:
        content = cast(dict, request.json) if request.is_json else {}
        current_password = content.get('current_password') if content else None
        if not current_password or not isinstance(current_password, str):
            return _err("current_password required", 400)
        cur_ext = next(
            (e for e, u in cast(dict, self.cfg.get('webui_users', {})).items() if u == username),
            None,
        )
        if not cur_ext:
            return _err("Account has no credentials set", 400)
        stored = cast(dict, self.cfg.get('webui_passwords', {})).get(cur_ext, '')
        if not stored or not hmac.compare_digest(stored, self.sub.hash(current_password)):
            return _err("Invalid current password", 401)
        try:
            self.sub.delete_user(
                username=username,
                perma=True
            )
            
            resp, code = _ok(msg="Deleted account")
            resp.set_cookie(
                'token',
                '',
                max_age=0,
                httponly=True,
                secure=True,
                samesite='Lax'
            )
            return resp, code
        except Exception as e:
            self.log.error(f"/delete {username}: {str(e)}")
            return _err("Internal server error", 500)
    def logout(self) -> Response:
        resp, code = _ok(msg="Logged out")
        resp.set_cookie(
            'token', 
            '', 
            max_age=0, 
            httponly=True, 
            secure=True, 
            samesite='Lax'
        )
        return resp
    @requires_webapi_auth
    def settings(self, username) -> ResponseType:
        """Update users display name or fingerprint.
        Changing ext_username or ext_password requires `current_password` in body."""
        content = cast(dict, request.json)
        displayname = content.get('name', None)
        fingerprint = content.get('fingerprint', None)
        ext_username = content.get('username', None)
        ext_password = content.get('password', None)
        current_password = content.get('current_password', None)
        if fingerprint:
            if fingerprint not in self.cfg['fingerprints']:
                return _err("Unknown fingerprint")
        if displayname:
            if len(displayname) > 16:
                return _err("displayname exceeds max. length of 16")
        if ext_username:
            if len(ext_username) > 32:
                return _err("username exceeds max. length of 32")
        # Credential changes require the current password (guards stolen cookies)
        if ext_username or ext_password:
            if not current_password or not isinstance(current_password, str):
                return _err("current_password required to change credentials", 400)
            cur_ext = next(
                (e for e, u in cast(dict, self.cfg.get('webui_users', {})).items() if u == username),
                None,
            )
            if not cur_ext:
                return _err("Account has no credentials set", 400)
            stored = cast(dict, self.cfg.get('webui_passwords', {})).get(cur_ext, '')
            if not stored or not hmac.compare_digest(stored, self.sub.hash(current_password)):
                return _err("Invalid current password", 401)
        try:
            x = self.sub.update_params(
                username=username,
                ext_username=ext_username,
                ext_password=ext_password,
                displayname=displayname,
                fingerprint=fingerprint
            )
            if isinstance(x, str):
                return _err(x, 400)
            return _ok()
        except Exception as e:
            self.log.critical(e)
            return _err("Internal server error", 500)
    @requires_webapi_auth
    def reset(self, username) -> ResponseType:
        """Reset token and UUID (api wrapper)"""
        try:
            x = self.sub.reset_user(username)
            if not isinstance(x, dict):
                return _err("Internal server error", 500)
            return _ok(obj=x)
        except Exception as e:
            self.log.critical(e)
            return _err("Internal server error", 500)
    @requires_webapi_auth
    @requires_fields('code')
    def bonus(self, username) -> ResponseType:
        """Apply bonus code."""
        content = request.json
        
        try:
            result = self.sub.apply_bonus_code(
                username=username,
                code=cast(str, content["code"]),
            )
        except Exception as e:
            self.log.critical(f"bonus failed: {e}")
            return _err("Internal server error", 500)
    
        if isinstance(result, str):
            if result == "Unknown code":
                return _err(result, 404)
            if result == "Unknown user":
                return _err(result, 404)
            return _err(result, 400)
    
        return _ok(obj=result)
    @requires_webapi_auth
    def stats(self, username) -> ResponseType:
        """Get user info from token"""
        bandwidths = self.sub.bandwidth(username)
        wl_bandwidths = self.sub.bandwidth(username, whitelist=True)
        obj = {
            "_": random.choice(cast(list, self.cfg.get('funny_strings', []))),
            "token": self.cfg['tokens'][username],
            "link": f"https://pomi.lol/sub?token={self.cfg['tokens'][username]}",
            "displayname": self.cfg['displaynames'][username], # No leaking internal usernames!
            "uuid": self.cfg['users'][username],
            "fingerprint": self.cfg['userFingerprints'][username],
            "enabled": self.cfg['status'][username],
            "wl_enabled": self.cfg['statusWl'][username],
            "time": self.cfg['time'][username],
            "online": self.sub.is_online(username),
            "bandwidth": {
                "total": {
                    "upload": bandwidths[0],
                    "download": bandwidths[1]
                },
                "wl_total": {
                    "upload": wl_bandwidths[0],
                    "download": wl_bandwidths[1]
                },
                "monthly": self.cfg['bw'][username][1],
                "wl_monthly": self.cfg['wl_bw'].get(username, [0,0])[1],
                "limit": self.cfg['bw'][username][0],
                "wl_limit": self.cfg['wl_bw'].get(username, [0,0])[0]
            }
        }
        return _ok(obj=obj)

    @requires_no_auth
    @requires_fields('username', 'password', 'code', 'name')
    def register(self) -> ResponseType:
        """Register a new user via code."""
        content = request.json    
        try:
            result = self.sub.register_with_code(
                code=cast(str, content['code']),
                username=f"web_{uuid.uuid4().hex[:16]}",
                displayname=cast(str, content['name']),
                ext_username=cast(str, content['username']),
                ext_password=cast(str, content['password']),
            )
        except Exception as e:
            self.log.critical(f"register failed: {e}")
            return _err("Internal server error", 500)
    
        if isinstance(result, str):
            if result in {"Invalid code", "Username exists", "Ext Username exists"}:
                return _err(result, 403)
            return _err(result, 400)
    
        return _ok("Created", 201, result)

    @requires_fields('username', 'password')
    def login(self) -> ResponseType:
        """Get the cookie for auth."""
        content = cast(dict, request.json)
        internal = self.validate_credentials(content['username'], content['password'])
        if not internal:
            return _err("Invalid credentials.", 401)
        token = self.cfg['tokens'][internal]
        r, code = _ok(msg="Successful login", obj={"username": content['username']})
        r.set_cookie(
            key='token',
            value=token,
            max_age=30 * 24 * 3600,
            httponly=True,
            secure=True,
            samesite='Lax'
        )
        return r, code


# class Api: 
# Rewrite later
