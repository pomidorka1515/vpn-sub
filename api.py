from core import Subscription, BWatch, nginx_404
from config import Config
from loggers import Logger

import threading
import time
import uuid
import random
import datetime
import decimal
import hmac

from functools import wraps
from flask import Flask, Response, jsonify, send_file, redirect, request, make_response
from typing import cast, Tuple, Any, Self, Callable

def _ok(
    code: int = 200,
    msg: str | None = None,
    obj: str | int | float | bool | dict | list | tuple | uuid.UUID | decimal.Decimal | \
        datetime.datetime | datetime.time | datetime.timedelta | datetime.date \
        | None = None
) -> Tuple[Response, int]:
    """Internal helper function to return an error Response."""
    return jsonify({"success": True, "msg": msg, "obj": obj}), code

def _err(
    msg: str | None = None,
    code: int = 400,
    obj: str | int | float | bool | dict | list | tuple | uuid.UUID | decimal.Decimal | \
        datetime.datetime | datetime.time | datetime.timedelta | datetime.date \
        | None = None
) -> Tuple[Response, int]:
    """Internal helper function to return a successful Response."""
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

class WebApi:
    """Public api for web ui.
    Dependencies: Subscription, BWatch
    Classes depending on this: none"""
    def __init__(self,
                 app: Flask,
                 cfg: Config,
                 sub: Subscription,
                 bw: BWatch):
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg = cfg
            self.app = app
            self.sub = sub
            self.bw = bw
            self.uri = f"/{self.cfg['uri']}/webapi" 
            self._rl_data = {}
            self._rl_lock = threading.Lock()
            self.reg_handles()
    
    def rate_limit(self, max_requests: int):
        """Rate-limiting decorator."""
        def decorator(f):
            @wraps(f) 
            def wrapped(*args, **kwargs):
                ip = request.headers.get('X-Real-IP', request.remote_addr)
                now = time.time()
                

                with self._rl_lock:
                    stale = [k for k, v in self._rl_data.items() if v and now - v[-1] > 60]
                    for k in stale:
                        del self._rl_data[k]
                    
                    if ip not in self._rl_data:
                        self._rl_data[ip] = []
                    self._rl_data[ip] = [t for t in self._rl_data[ip] if now - t < 60]
                    if len(self._rl_data[ip]) >= max_requests:

                        return _err(msg="Too many requests.", code=429)
                    self._rl_data[ip].append(now)
                
                return f(*args, **kwargs)
            return wrapped
        return decorator    
    def reg_handles(self):
        @self.app.route(f"{self.uri}/register", methods=['POST'])
        @self.rate_limit(5)
        def _register():
            return self.register()
        @self.app.route(f"{self.uri}/login", methods=['POST'])
        @self.rate_limit(10)
        def _login():
            return self.login()
        @self.app.route(f"{self.uri}/bonus", methods=['POST'])
        @self.rate_limit(15)
        def _apply_bonus():
            return self.bonus()
        @self.app.route(f"{self.uri}/stats")
        @self.rate_limit(20)
        def _stats():
            return self.stats()
        @self.app.route(f"{self.uri}/reset", methods=['POST'])
        @self.rate_limit(5)
        def _reset():
            return self.reset()
        @self.app.route(f"{self.uri}/settings", methods=['POST'])
        @self.rate_limit(15)
        def _settings():
            return self.settings()
        @self.app.route(f"{self.uri}/logout", methods=['POST'])
        @self.rate_limit(20)
        def _logout():
            return self.logout()
        @self.app.route(f"{self.uri}/fingerprints")
        @self.rate_limit(60)
        def _fps():
            return self.fps()
        @self.app.route(f"{self.uri}/delete", methods=['DELETE'])
        @self.rate_limit(3)
        def _delete():
            return self.delete()
        @self.app.route(f"{self.uri}/validate", methods=['POST'])
        @self.rate_limit(60)
        def _validate_username():
            return self.validate_username()
        @self.app.route(f"{self.uri}/profiles", methods=['POST'])
        @self.rate_limit(60)
        def _profiles():
            return self.profiles()
        @self.app.route(f"/sub/panel")
        def _panel():
            return self.panel()
    
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
        if hmac.compare_digest(users_pw[username], self.sub.hash(password)):
            return False
        webui_users = cast(dict, self.cfg.get('webui_users', {}))
        internal = webui_users.get(username)
        if not internal or internal not in self.cfg['users']:
            return False
        return internal
    @requires_fields('username')
    def validate_username(self) -> Tuple[Response, int] | Response:
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
    def panel(self) -> Response:
        token = request.cookies.get('token')
        if not self.validate_token(token):
            return make_response(redirect('/sub/auth'))
        return send_file('/var/www/sub/new/res/dashboard.html', etag=False)

    @requires_webapi_auth
    @requires_fields('lang')
    def profiles(self, username) -> Tuple[Response, int] | Response:
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
    def fps(self, username) -> Tuple[Response, int] | Response:
        return _ok(obj=self.cfg['fingerprints'])
    @requires_webapi_auth
    def delete(self, username) -> Tuple[Response, int] | Response:
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
    def settings(self, username) -> Tuple[Response, int] | Response:
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
        except:
            return _err("Internal server error", 500)
    @requires_webapi_auth
    def reset(self, username) -> Tuple[Response, int] | Response:
        """Reset token and UUID (api wrapper)"""
        try:
            x = self.sub.reset_user(username)
            if not isinstance(x, dict):
                return _err("Internal server error", 500)
            return _ok(obj=x)
        except:
            return _err("Internal server error", 500)
    @requires_webapi_auth
    @requires_fields('code')
    def bonus(self, username) -> Tuple[Response, int] | Response:
        """Apply bonus code"""

        content = cast(dict, request.json)
        codeobj = self.sub.consume_code(content['code'])
        if not isinstance(codeobj, dict):
            return _err("Unknown code", 404)
        if codeobj['action'] != 'bonus':
            return _err("Unknown code", 404)
        days = codeobj['days']
        gb = codeobj['gb']
        wl_gb = codeobj['wl_gb']
        if self.cfg['time'][username] == 0:
            days = 0
        else:
            days = self.cfg['time'][username] + days * 86400
        if self.cfg['bw'][username][0] == 0:
            gb = 0
        else:
            gb = self.cfg['bw'][username][0] + gb
        if self.cfg['wl_bw'][username][0] == 0:
            wl_gb = 0
        else:
            wl_gb = self.cfg['wl_bw'][username][0] + wl_gb
        obj = {"days": codeobj['days'], "gb": codeobj['gb'], "wl_gb": codeobj['wl_gb'], "perma": codeobj['perma']}
        try:
            self.sub.update_params(
                username=username,
                limit=gb,
                wl_limit=wl_gb,
                timee=days
            )
            return _ok(obj=obj)
        except:
            return _err("Internal server error", 500)
    @requires_webapi_auth
    def stats(self, username) -> Tuple[Response, int] | Response:
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
    def register(self) -> Tuple[Response, int] | Response:
        """Register a new user via code"""
        content = cast(dict, request.json)
        code = cast(str, content.get('code'))
        codeobj = self.sub.consume_code(code=code)
        if not isinstance(codeobj, dict): # action days gb
            return _err("Invalid code", 403)
        if codeobj['action'] != 'register':
            return _err("Invalid code", 403)
        # Sanitize: usernames allowlist-only, displaynames blocklist
        l_displayname = self.sub.FILTERS.get('displayname', '')

        displayname = cast(str, content.get('name')).translate(str.maketrans('', '', l_displayname))
        ext_username = self.sub.sanitize(cast(str, content.get('username')))
        uid_short = uuid.uuid4().hex[:16]
        internal_username = f"web_{uid_short}"
        days = codeobj['days']
        gb = codeobj['gb']
        # Create the user
        if len(displayname) > 16:
            return _err("displayname exceeds max. length of 16")
        if len(ext_username) > 32:
            return _err("username exceeds max. length of 32")

        # Fast-path pre-check for nicer error. The real atomic check is inside add_new_user.
        if ext_username in self.cfg['webui_users']:
            return _err("Username already exists", 403)
        try:
            x = self.sub.add_new_user(
                username=internal_username,
                displayname=displayname,
                ext_username=ext_username,
                ext_password=content.get('password'),
                token=None, # Let method generate
                limit=gb, # No conversion needed (in GB alrrady)
                timee=int(time.time() + days * 86400) if days else 0 # Current time in UTC (plus day amount)
            )
            if isinstance(x, str):
                # Atomic uniqueness check lost the race, or validation failed
                if "exists" in x.lower():
                    return _err("Username already exists", 403)
                return _err(x, 400)
            if not isinstance(x, dict):
                return _err("Internal server error", 500)
            return _ok(201, "Created", x)
        except Exception as e:
            self.log.critical(f"error: {str(e)}")
            return _err("Internal server error", 500)
    @requires_fields('username', 'password')
    def login(self) -> Tuple[Response, int] | Response:
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


class Api:
    def __init__(self,
                 app: Flask,
                 cfg: Config,
                 sub: Subscription,
                 bw: BWatch):
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg = cfg
            self.app = app
            self.sub = sub
            self.bw = bw
            self.nginx404 = nginx_404
            self.resp = Response(self.nginx404, status=404, mimetype='text/html')
            self.resp.headers['Content-Type'] = "text/html"
            self.uri = self.cfg['uri']
            self.token = self.cfg['api_token']
            self.reg_handles()
    
    def require_auth(self, t) -> bool:
            return t == self.token

    def reg_handles(self):
        @self.app.errorhandler(404)
        def _handle_404(e):
            return self.handle_404(e)
        @self.app.errorhandler(405)
        def _handle_405(e):
            return self.handle_405(e)
        
        @self.app.route(f"/{self.uri}/api/user/list", methods=['GET'])
        def _api_list():
            return self.api_list()
        @self.app.route(f"/{self.uri}/api/user/add", methods=['POST'])
        def _api_add():
            return self.api_add()
        @self.app.route(f"/{self.uri}/api/user/refresh", methods=['GET'])
        def _api_refresh():
            return self.api_refresh()
        @self.app.route(f"/{self.uri}/api/user/delete", methods=['POST'])
        def _api_delete():
            return self.api_delete()
        @self.app.route(f"/{self.uri}/api/user/info", methods=['POST'])
        def _api_info():
            return self.api_info()
        @self.app.route(f"/{self.uri}/api/user/isonline", methods=['POST'])
        def _api_isonline():
            return self.api_isonline()
        @self.app.route(f"/{self.uri}/api/user/reset", methods=['POST'])
        def _api_reset():
            return self.api_reset()
        @self.app.route(f"/{self.uri}/api/user/change", methods=['POST'])
        def _api_change():
            return self.api_change()
        @self.app.route(f"/{self.uri}/api/user/onlines", methods=['GET'])
        def _api_onlines():
            return self.api_onlines()
        @self.app.route(f"/{self.uri}/api/code/get", methods=['POST'])
        def _api_code_get():
            return self.api_code_get()
        @self.app.route(f"/{self.uri}/api/code/delete", methods=['POST'])
        def _api_code_delete():
            return self.api_code_delete()
        @self.app.route(f"/{self.uri}/api/code/add", methods=['POST'])
        def _api_code_add():
            return self.api_code_add()
        @self.app.route(f"/{self.uri}/api/code/list", methods=['GET'])
        def _api_code_list():
            return self.api_code_list()
        
    def handle_404(self, e = ""):
        return self.resp
    def handle_405(self, e = ""):
        return self.resp
