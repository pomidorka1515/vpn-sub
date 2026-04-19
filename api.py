from __future__ import annotations

from core import Subscription, BWatch, nginx_404
from config import Config
from loggers import Logger

import threading
import time
import uuid
import random

from functools import wraps
from flask import Flask, Response, jsonify, send_file, redirect, request, make_response
from abc import ABC
from typing import cast, Tuple, Any, Callable, Literal, NamedTuple

__all__ = ['WebApi', 'Api', 'BaseApi']

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
    """A Flask route, used in BaseApi."""
    method: HTTPMethod
    path: str
    handler: str
    rate_limit: int | None = None

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
        provided = request.headers.get('Authorization', '')
        if not provided or not self.sub.compare(provided, self.token):
            return _err("Unauthorized", 401)
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


def requires_fields(*fields: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Validate request.json has all named fields. Returns 400 on failure."""
    def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(f)
        def wrapper(*args, **kwargs):
            content = request.get_json(silent=True)
            if content is None:
                return _err("Missing JSON data.", 400)
            if not isinstance(content, dict):
                return _err("Body must be a JSON dict.", 400)
            missing = [x for x in fields if x not in content]
            if missing:
                return _err(f"Missing fields: {', '.join(missing)}", 400)
            return f(*args, **kwargs)
        return wrapper
    return decorator

def requires_args(*arg: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Validate request.args has all named fields. Returns 400 on failure."""
    def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(f)
        def wrapper(*args, **kwargs):
            content = request.args
            if content is None:
                return _err("Missing data.", 400)
            missing = [x for x in arg if x not in content]
            if missing:
                return _err(f"Missing args: {', '.join(missing)}", 400)
            return f(*args, **kwargs)
        return wrapper
    return decorator



class WebApi(BaseApi):
    """Public api for web ui.
    Dependencies: Subscription, BWatch
    Classes depending on this: none"""
    _REDIRECT_HTML = """<!DOCTYPE html><html><head><title>Opening...</title></head><body><p>Redirecting... If nothing happens, <a id="manual-link" href="#">click here</a>.</p><script>const urlParams=new URLSearchParams(window.location.search);const subUrl=urlParams.get('url')||'';const prefix=urlParams.get('prefix')||'';const link=prefix+subUrl;document.getElementById('manual-link').href=link;window.location.replace(link)</script></body></html>"""

    ROUTES: list[Route] = [
        Route('GET', '/redirect', 'redirect_page', None),
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
        Route('GET', '/panel', 'gui_panel', None),
        Route('GET', '/auth', 'gui_auth', None),
        Route('GET', '/webapi/qr', 'qr', 80)
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
        if not self.sub.compare(users_pw[username], self.sub.hash(password)):
            return False
        webui_users = cast(dict, self.cfg.get('webui_users', {}))
        internal = webui_users.get(username)
        if not internal or internal not in self.cfg['users']:
            return False
        return internal
    def redirect_page(self) -> ResponseType:
        prefix = request.args.get('prefix', '')
        if not (prefix.startswith('happ://') or prefix.startswith('v2ray') or prefix.startswith('clash')):
            return _err("Invalid prefix", 400)
        return Response(self._REDIRECT_HTML, mimetype='text/html')

    def gui_panel(self) -> ResponseType:
        token = request.cookies.get('token')
        if not self.validate_token(token):
            return make_response(redirect('/sub/auth'))
        return send_file('/var/www/sub/new/res/dashboard.html', etag=False)
    def gui_auth(self) -> ResponseType:
        return send_file('/var/www/sub/new/res/auth.html', etag=False)

    @requires_webapi_auth
    def qr(self, username):
        lang = request.args.get('lang', 'en')
        if lang not in ['en', 'ru']:
            lang = 'en'
        
        token = self.cfg['tokens'][username]
        link = f"https://pomi.lol/sub?token={token}&lang={lang}"
        
        if request.args.get('happ') == '1':
            link = f"happ://add/{link}"
        
        try:
            buf = self.sub.make_qr(link)
        except ValueError as e:
            return _err(str(e), 400)
        
        response = make_response(send_file(buf, mimetype='image/png'))
        response.headers['Cache-Control'] = 'private, max-age=300'
        return response

    def validate_username(self) -> ResponseType:
        """Check if a username is valid (no illegal chars)"""
        raw = request.args.get('username', None)
        if not raw:
            return _err("'username' field is missing")
        if not isinstance(raw, str):
            return _err("'username' must be a string")
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
    def profiles(self, username) -> ResponseType:
        lang = request.args.get('lang')
        if lang not in ['ru', 'en']:
            return _err("Unknown language", 400)

        index = 0 if lang == 'en' else 1
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
        if not stored or not self.sub.compare(stored, self.sub.hash(current_password)):
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
    @requires_webapi_auth
    def logout(self) -> ResponseType:
        resp, code = _ok(msg="Logged out")
        resp.set_cookie(
            'token', 
            '', 
            max_age=0, 
            httponly=True, 
            secure=True, 
            samesite='Lax'
        )
        return resp, code
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
            if not stored or not self.sub.compare(stored, self.sub.hash(current_password)):
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
        content = cast(dict, request.json)
        
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
        return _ok(obj=self.sub.get_info(username))


    @requires_no_auth
    @requires_fields('username', 'password', 'code', 'name')
    def register(self) -> ResponseType:
        """Register a new user via code."""
        content = cast(dict, request.json)
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
    

class Api(BaseApi):
    ROUTES = [
        Route('GET', '/api/user/list', 'user_list'),
        Route('GET', '/api/user/info', 'user_info'),
        Route('POST', '/api/user/add', 'user_add'),
        Route('POST', '/api/user/delete', 'user_delete'),
        Route('GET', '/api/user/refresh', 'user_refresh'),
        Route('GET', '/api/user/onlines', 'user_onlines'),
        Route('POST', '/api/user/reset', 'user_reset'),


        Route('GET', '/api/panel/status', 'panel_status'),


        Route('GET', '/api/code/list', 'code_list'),
        Route('GET', '/api/code/info', 'code_info'),
        Route('POST', '/api/code/add', 'code_add'),
        Route('POST', '/api/code/delete', 'code_delete')
    ]

    def __init__(self,
                 app: Flask,
                 cfg: Config,
                 sub: Subscription,
                 bw: BWatch):
        self.log = Logger(type(self).__name__)
        uri = f"/sub/{cfg['api_uri']}"
        self.token = cfg['api_token']
        super().__init__(app, cfg, sub, bw, uri)

    # TODO: make this
    @requires_admin_auth
    def user_list(self) -> ResponseType: 
        users = list(self.cfg['users'].keys())
        if not users:
            return _ok(obj=[])
        return _ok(obj=users)
        
    
    @requires_admin_auth
    @requires_args('user')
    def user_info(self) -> ResponseType: 
        username = request.args['user']
        pretty = request.args.get('beautify', '').lower() in ('1', 'true', 'yes')
        return _ok(obj=self.sub.get_info(username=username, pretty=pretty))
 
    @requires_admin_auth
    @requires_fields('user', 'displayname')
    def user_add(self) -> ResponseType: 
        try:
            content = cast(dict, request.json)
            username = cast(str, content.get('user'))
            if self.sub.isuser(username):
                return _err("Username exists")
            x = self.sub.add_new_user(
                username=username,
                displayname=cast(str, content.get('displayname', None)),
                ext_username=cast(str, content.get('ext_username', None)),
                ext_password=cast(str, content.get('ext_password', None)),
                token=cast(str, content.get('token', None)),
                userid=cast(str, content.get('userid', None)),
                fingerprint=cast(str, content.get('fingerprint', None)),
                limit=cast(int, content.get('limit', 0)),
                wl_limit=cast(int, content.get('wl_limit', 5)),
                timee=cast(int, content.get('time', 0))
            )
            if not isinstance(x, dict):
                return _err(msg=x)
            return _ok("Created", 201)
        except Exception as e:
            self.log.error(f"user_add: {e}")
            return _err(f"Error: {str(e)}", 500)

    @requires_admin_auth
    @requires_fields('user')
    def user_delete(self) -> ResponseType:
        try:
            content = cast(dict, request.json)
            username = cast(str, content.get('user'))
            perma = cast(bool, content.get('perma', True))
            if not self.sub.isuser(username):
                return _err("Unknown username")
            self.sub.delete_user(username, perma)
            return _ok("Deleted")
        except Exception as e:
            self.log.error(f"user_delete: {e}")
            return _err(f"Error: {str(e)}", 500)

    @requires_admin_auth
    def user_refresh(self) -> ResponseType:
        try:
            for cc in self.cfg['users'].keys():
                self.sub.add_users(cc)
            return _ok("Refreshed all users.")
        except Exception as e:
            self.log.error(f"user_refresh: {e}")
            return _err(f"Error: {str(e)}", 500)
    
    @requires_admin_auth
    def user_onlines(self) -> ResponseType:
        new = request.args.get('keyed', '').lower() in ('1', 'true', 'yes')
        online_users = cast(dict, self.sub.get_online_users(new = new))
        if not online_users:
            return _ok(obj=[])
        return _ok(obj=online_users)

    @requires_admin_auth
    @requires_fields('user')
    def user_reset(self) -> ResponseType:
        try:
            content = cast(dict, request.json)
            username = cast(str, content.get('user'))
            if not self.sub.isuser(username):
                return _err("Unknown username")
            x = self.sub.reset_user(username)
            return _ok(obj=x)
        except Exception as e:
            self.log.error(f"user_reset fail: {e}")
            return _err(str(e), 500)
            
    @requires_admin_auth
    def panel_status(self) -> ResponseType: return _err("Not implemented", 501)
    
    @requires_admin_auth
    def code_list(self) -> ResponseType: return _err("Not implemented", 501)
    
    @requires_admin_auth
    @requires_fields('code')
    def code_info(self) -> ResponseType: return _err("Not implemented", 501)
    
    @requires_admin_auth
    @requires_fields('code', 'action')
    def code_add(self) -> ResponseType: return _err("Not implemented", 501)
    
    @requires_admin_auth
    @requires_fields('code')
    def code_delete(self) -> ResponseType: return _err("Not implemented", 501)

