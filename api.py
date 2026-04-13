from core import Subscription, BWatch, nginx_404
from config import Config
from loggers import Logger

import threading
import time
import uuid
import random

from functools import wraps
from flask import Flask, Response, jsonify, send_file, redirect, request, make_response
from typing import cast, Tuple

def validate_fields(*fields) -> str | None:
    """Module-level method to verify fields for the APIs."""
    content = request.json
    if content is None:
        return "Missing JSON Data."
    for field in fields:
        if field not in content:
            return f"Missing '{field}' key in JSON."
    return None

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
        self.log.debug("loading Public Api")
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
                        return jsonify({"success": False, "msg": "429 Too many requests", "obj": None}), 429
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
        if users_pw[username] != self.sub.hash(password):
            return False
        webui_users = cast(dict, self.cfg.get('webui_users', {}))
        internal = webui_users.get(username)
        if not internal or internal not in self.cfg['users']:
            return False
        return internal
    def _inv_t(self) -> Tuple[Response, int] | Response:
        return jsonify({"success": False, "msg": "Invalid token.", "obj": None}), 401

    def validate_username(self) -> Tuple[Response, int] | Response:
        """Check if a username is valid (no illegal chars). POST {"username": "..."}"""
        err = validate_fields('username')
        if err:
            return jsonify({"success": False, "msg": err, "obj": None}), 400
        content = cast(dict, request.json)
        raw = cast(str, content.get('username'))
        sanitized = self.sub.sanitize(raw)
        valid = raw == sanitized and len(raw) > 0
        taken = sanitized in cast(dict, self.cfg.get('webui_users', {}))
        return jsonify({"success": True, "msg": None, "obj": {
            "MAX_LENGTH": 32,
            "valid": valid,
            "taken": taken,
            "sanitized": sanitized
        }})
    def panel(self) -> Response:
        token = request.cookies.get('token')
        if not self.validate_token(token):
            return make_response(redirect('/sub/auth'))
        return send_file('/var/www/sub/new/res/dashboard.html')

    def profiles(self) -> Tuple[Response, int] | Response:
        token = request.cookies.get('token')
        username = cast(str, self.validate_token(token))
        if not username:
            return self._inv_t()
        err = validate_fields('lang')
        if err:
            return jsonify({"success": False, "msg": err, "obj": None}), 400
        content = cast(dict, request.json)
        if content['lang'] not in ['ru', 'en']:
            return jsonify({"success": False, "msg": "Unknown language", "obj": None}), 400

        index = 0 if content['lang'] == 'en' else 1
        obj = {}
        for i_name, name in self.cfg['profiles'].items():
            name = name[index]
            desc = self.cfg['profileDescriptions'][i_name][index]
            obj[name] = desc
        return jsonify({"success": True, "msg": None, "obj": obj})
    def fps(self) -> Tuple[Response, int] | Response:
        token = request.cookies.get('token')
        username = cast(str, self.validate_token(token))
        if not username:
            return self._inv_t()
        return jsonify({"success": True, "msg": None, "obj": self.cfg['fingerprints']}) 
    def delete(self) -> Tuple[Response, int] | Response:
        token = request.cookies.get('token')
        username = cast(str, self.validate_token(token))
        if not username:
            return self._inv_t()
        
        try:
            self.sub.delete_user(
                username=username,
                perma=True
            )

            resp = jsonify({"success": True, "msg": "Deleted account", "obj": None})
            resp.set_cookie(
                'token',
                '',
                max_age=0,
                httponly=True,
                secure=True,
                samesite='Lax'
            )
            return resp
        except Exception as e:
            self.log.error(f"/delete {username}: {str(e)}")
            return jsonify({"success": False, "msg": "Internal server error", "obj": None}), 500
    def logout(self) -> Response:
        resp = jsonify({"success": True, "msg": "Logged out", "obj": None})
        resp.set_cookie(
            'token', 
            '', 
            max_age=0, 
            httponly=True, 
            secure=True, 
            samesite='Lax'
        )
        return resp
    def settings(self) -> Tuple[Response, int] | Response:
        """Update users display name or fingerprint."""
        token = request.cookies.get('token')
        username = self.validate_token(token)
        if not username:
            return self._inv_t() # Guard!
        # not needed err = validate_fields('displayname')
        content = cast(dict, request.json)
        displayname = content.get('name', None)
        fingerprint = content.get('fingerprint', None)
        ext_username = content.get('username', None)
        ext_password = content.get('password', None)
        if fingerprint:
            if fingerprint not in self.cfg['fingerprints']:
                return jsonify({"success": False, "msg": "Unknown fingerprint", "obj": None}), 400
        if displayname:
            if len(displayname) > 16:
                return jsonify({"success": False, "msg": "displayname exceeds max. length of 16", "obj": None}), 400
        if ext_username:
            if len(ext_username) > 32:
                return jsonify({"success": False, "msg": "username exceeds max. length of 32", "obj": None}), 400
        try:
            x = self.sub.update_params(
                username=username,
                ext_username=ext_username,
                ext_password=ext_password,
                displayname=displayname,
                fingerprint=fingerprint
            )
            if isinstance(x, str):
                return jsonify({"success": False, "msg": x, "obj": None}), 400
            return jsonify({"success": True, "msg": None, "obj": None})
        except:
             return jsonify({"success": False, "msg": "Internal server error", "obj": None}), 500
    def reset(self) -> Tuple[Response, int] | Response:
        """Reset token and UUID (api wrapper)"""
        token = request.cookies.get('token')
        username = self.validate_token(token)
        if not username:
            return self._inv_t()

        try:
            x = self.sub.reset_user(username)
            if not isinstance(x, dict):
                return jsonify({"success": False, "msg": "Internal server error", "obj": None}), 500
            return jsonify({"success": True, "msg": None, "obj": x})
        except:
            return jsonify({"success": False, "msg": "Internal server error", "obj": None}), 500
    def bonus(self) -> Tuple[Response, int] | Response:
        """Apply bonus code"""
        token = request.cookies.get('token')
        username = self.validate_token(token)
        if not username:
            return self._inv_t()
        err = validate_fields('code')
        if err:
            return jsonify({"success": False, "msg": err, "obj": None}), 400

        content = cast(dict, request.json)
        codeobj = self.sub.consume_code(content['code'])
        if not isinstance(codeobj, dict):
            return jsonify({"success": False, "msg": "Unknown code", "obj": None}), 400
        if codeobj['action'] != 'bonus':
            return jsonify({"success": False, "msg": "Unknown code", "obj": None}), 400
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
            return jsonify({"success": True, "msg": None, "obj": obj})
        except:
            return jsonify({"success": False, "msg": "Internal server error", "obj": None}), 500
    def stats(self) -> Tuple[Response, int] | Response:
        """Get user info from token"""
        token = request.cookies.get('token')
        username = self.validate_token(token)
        if not username:
            return self._inv_t()
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
        return jsonify({"success": True, "msg": None, "obj": obj})
    def register(self) -> Tuple[Response, int] | Response:
        """Register a new user via code"""
        err = validate_fields('username', 'password', 'code', 'name')
        if err:
            return jsonify({"success": False, "msg": err, "obj": None}), 400
        token = request.cookies.get('token')
        if token:
            return jsonify({"success": False, "msg": "Must not be authorized.", "obj": None}), 403
        content = cast(dict, request.json)
        code = cast(str, content.get('code'))
        codeobj = self.sub.get_code(code=code)
        if not isinstance(codeobj, dict): # action days gb
            return jsonify({"success": False, "msg": "Invalid code", "obj": None}), 403
        if codeobj['action'] != 'register':
            return jsonify({"success": False, "msg": "Invalid code", "obj": None}), 403
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
            return jsonify({"success": False, "msg": "displayname exceeds max. length of 16", "obj": None}), 400
        if len(ext_username) > 32:
            return jsonify({"success": False, "msg": "username exceeds max. length of 32", "obj": None}), 400

        if ext_username in self.cfg['webui_users']:
            return jsonify({"success": False, "msg": "Username already exists", "obj": None}), 403
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
            if not isinstance(x, dict):
                return jsonify({"success": False, "msg": "Internal server error", "obj": None}), 500
            if not codeobj.get('perma', False): self.sub.delete_code(code)
            return jsonify({"success": True, "msg": "Created", "obj": x}), 201
        except Exception as e:
            self.log.critical(f"error: {str(e)}")
            return jsonify({"success": False, "msg": "Internal server error", "obj": None}), 500
    def login(self) -> Tuple[Response, int] | Response:
        """Get the cookie for auth."""
        err = validate_fields('username', 'password')
        if err:
            return jsonify({"success": False, "msg": err, "obj": None}), 400
        content = cast(dict, request.json)
        internal = self.validate_credentials(content['username'], content['password'])
        if not internal:
            return jsonify({"success": False, "msg": "Invalid credentials.", "obj": None}), 401
        token = self.cfg['tokens'][internal]
        r = jsonify({"success": True, "msg": "Successful login", "obj": {"username": content['username']}})
        r.set_cookie(
            key='token',
            value=token,
            max_age=30 * 24 * 3600,
            httponly=True,
            secure=True,
            samesite='Lax'
        )
        return r        


class Api:
    def __init__(self,
                 app: Flask,
                 cfg: Config,
                 sub: Subscription,
                 bw: BWatch):
        self.log = Logger(type(self).__name__)
        self.log.debug("loading Api")
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
        
        self.log.info("loaded Api")
    def handle_404(self, e = ""):
        return self.resp
    def handle_405(self, e = ""):
        return self.resp

    def api_reset(self) -> Tuple[Response, int] | Response:
        if not self.require_auth(request.headers.get('Authorization')):
            return self.resp
        err = validate_fields('username')
        if err:
            return jsonify({"success": False, "msg": err, "obj": None}), 400
        try:
            obj = self.sub.reset_user(cast(dict, request.json).get('username', ''))
            if not isinstance(obj, dict):
                return jsonify({"success": False, "msg": "Unknown username", "obj": None})
            return jsonify({"success": True, "msg": None, "obj": obj})
        except Exception as e:
            return jsonify({"success": False, "msg": str(e), "obj": None})
   
    def api_onlines(self) -> Tuple[Response, int] | Response:
        if not self.require_auth(request.headers.get('Authorization')):
            return self.resp
        try:
            obj = self.sub.get_online_users()
            return jsonify({"success": True, "msg": None, "obj": obj})
        except Exception as e:
            return jsonify({"success": False, "msg": str(e), "obj": None}), 500
  
    def api_isonline(self) -> Tuple[Response, int] | Response:
        if not self.require_auth(request.headers.get('Authorization')):
            return self.resp
        err = validate_fields('username')
        if err:
            return jsonify({"success": False, "msg": err, "obj": None}), 400
        obj = self.sub.is_online(cast(dict, request.json).get('username', ''))
        if not isinstance(obj, bool):
            return jsonify({"success": False, "msg": "Unknown username", "obj": None}), 400
        return jsonify({"success": True, "msg": None, "obj": obj})
    
    def api_code_list(self) -> Tuple[Response, int] | Response:
        if not self.require_auth(request.headers.get('Authorization')):
            return self.resp
        
        obj = self.sub.list_code()
        return jsonify({"success": True, "msg": None, "obj": obj})
    def api_code_add(self) -> Tuple[Response, int] | Response:
        if not self.require_auth(request.headers.get('Authorization')):
            return self.resp
        err = validate_fields('code', 'action')
        if err:
            return jsonify({"success": False, "msg": err, "obj": None}), 400

        content = cast(dict, request.json)
        code = content.get('code', '')
        action = content.get('action', '')
        days = content.get('days', 0)
        gb = content.get('gb', 0)
        wl_gb = content.get('wl_gb', 0)
        perma = content.get('perma', False)
        if action not in ["register", "bonus"]:
            return jsonify({"success": False, "msg": "action field must be either \"register\" or \"bonus\"", "obj": None}), 400

        try:
            obj = self.sub.add_code(
                code=code,
                action=action,
                permanent=perma,
                days=days,
                wl_gb=wl_gb,
                gb=gb
            )
            return jsonify({"success": True, "msg": None, "obj": None})
        except Exception as e:
            return jsonify({"success": False, "msg": str(e), "obj": None}), 500
    def api_code_delete(self) -> Tuple[Response, int] | Response:
        if not self.require_auth(request.headers.get('Authorization')):
            return self.resp
        err = validate_fields('code')
        if err:
            return jsonify({"success": False, "msg": err, "obj": None}), 400

        content = cast(dict, request.json)
        code = content.get('code', '')

        try:
            obj = self.sub.delete_code(code=code)
            if obj == False: # None is falsy too
                return jsonify({"success": False, "msg": "Code not found", "obj": None}), 400
            else:
                return jsonify({"success": True, "msg": None, "obj": None})
        except Exception as e:
            return jsonify({"success": False, "msg": str(e), "obj": None}), 500
        

    def api_code_get(self) -> Tuple[Response, int] | Response:
        if not self.require_auth(request.headers.get('Authorization')):
            return self.resp
        err = validate_fields('code')
        if err:
            return jsonify({"success": False, "msg": err, "obj": None}), 400

        content = cast(dict, request.json)
        code = content.get('code', '')

        obj = self.sub.get_code(code=code)
        if not isinstance(obj, dict):
            return jsonify({"success": False, "msg": "Code not found", "obj": None}), 400
        else:
            return jsonify({"success": True, "msg": None, "obj": obj})
    def api_list(self) -> Tuple[Response, int] | Response:
        if not self.require_auth(request.headers.get('Authorization')):
            return self.resp
        
        users = []
        for i, v in self.cfg['users'].items():
            users.append(i)
        return jsonify({"success": True, "msg": None, "obj": users})

    def api_add(self) -> Tuple[Response, int] | Response:
        if not self.require_auth(request.headers.get('Authorization')):
            return self.resp
        err = validate_fields('user')
        if err:
            return jsonify({"success": False, "msg": err, "obj": None}), 400
        
        content = cast(dict, request.json)
        timee = content.get('time', 0)
        limit = content.get('limit', 0)
        if content['user'] in self.cfg['users']: return jsonify({"success": False, "msg": "User already exists", "obj": None}), 400
        # try: uuid = T['users'][content['user']]
        # except: return {"success": False, "msg": "Unknown username.", "obj": None}
        # reference >> {"username": username, "token": token, "uuid": uuid, "fingerprint": fingerprint}
        try: result = self.sub.add_new_user(username=content['user'], 
                                   displayname=content['displayname'],
                                   limit=limit,
                                   timee=timee)
        except Exception as e: return jsonify({"success": False, "msg": f"error {str(e)}", "obj": None}), 500
        return jsonify({"success": True,"msg": None, "obj": result}), 201

    def api_refresh(self) -> Tuple[Response, int] | Response:
        if not self.require_auth(request.headers.get('Authorization')):
            return self.resp
        
        try:
            for cc, ccc in self.cfg['users'].items():
                self.sub.add_users(cc)
        except Exception as e:
            return jsonify({"success": False, "msg": str(e), "obj": None}), 400
        return jsonify({"success": True,"msg": None, "obj": None})

    def api_delete(self) -> Tuple[Response, int] | Response:
        if not self.require_auth(request.headers.get('Authorization')):
            return self.resp
        err = validate_fields('user')
        if err:
            return jsonify({"success": False, "msg": err, "obj": None}), 400
        
        content = cast(dict, request.json)
        try: userid = self.cfg['users'][content['user']]
        except KeyError: return jsonify({"success": False, "msg": "Unknown username.", "obj": None}), 400
        try: self.sub.delete_user(username=content['user'],
                             perma=content['perma'])
        except Exception as e: return jsonify({"success": False, "msg": str(e), "obj": None}), 500
        return jsonify({"success": True,"msg": None, "obj": None})

    def api_info(self) -> Tuple[Response, int] | Response:
        if not self.require_auth(request.headers.get('Authorization')):
            return self.resp
        err = validate_fields('user')
        if err:
            return jsonify({"success": False, "msg": err, "obj": None}), 400
        
        content = cast(dict, request.json)
        try: userid = self.cfg['users'][content['user']]
        except KeyError: return jsonify({"success": False, "msg": "Unknown username.", "obj": None}), 400

        bandwidths = self.sub.bandwidth(username=content['user'])
        wl_bandwidths = self.sub.bandwidth(username=content['user'], whitelist=True)
        obj = {
            "uuid": userid,
            "token": self.cfg['tokens'][content['user']],
            "bandwidth": {
                "raw_table": bandwidths,
                "upload": round(bandwidths[0] / 10**6, 2),
                "download": round(bandwidths[1] / 10**6, 2),
                "total": round(bandwidths[2] / 10**6, 2),
                "monthly": round(self.cfg['bw'][content['user']][1], 2),
                "limit": self.cfg['bw'][content['user']][0]
            },
            "wl_bandwidth": {
                "raw_table": wl_bandwidths,
                "upload": round(wl_bandwidths[0] / 10**6, 2),
                "download": round(wl_bandwidths[1] / 10**6, 2),
                "total": round(wl_bandwidths[2] / 10**6, 2),
                "monthly": round(self.cfg['wl_bw'][content['user']][1], 2),
                "limit": self.cfg['wl_bw'][content['user']][0]
            },
            "fingerprint": self.cfg['userFingerprints'][content['user']],
            "displayname": self.cfg['displaynames'][content['user']],
            "enable": self.cfg['status'][content['user']],
            "enableTime": self.cfg['statusTime'][content['user']],
            "enableWl": self.cfg['statusWl'][content['user']],
            "time": self.cfg['time'][content['user']],
            "online": self.sub.is_online(content['user'])
        }
        return jsonify({"success": True, "msg": None, "obj": obj})

    def api_change(self) -> Tuple[Response, int] | Response:
        if not self.require_auth(request.headers.get('Authorization')):
            return self.resp
        err = validate_fields('user')
        if err:
            return jsonify({"success": False, "msg": err, "obj": None}), 400
        
        content = cast(dict, request.json)
        displayname = content.get('displayname', None)
        token = content.get('token', None)
        fingerprint = content.get('fingerprint', None)
        limit = content.get('limit', None)
        timee = content.get('time', None)
        
        try:
            self.sub.update_params(
                username = content['user'],
                displayname = displayname,
                token = token,
                fingerprint = fingerprint,
                limit = limit,
                timee = timee
            )
        except Exception as e:
            return jsonify({"success": False, "msg": str(e), "obj": None})
        return jsonify({"success": True, "msg": None, "obj": None})
