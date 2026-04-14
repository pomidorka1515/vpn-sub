from __future__ import annotations

from loggers import Logger
from config import Config
# from bots import PublicBot

import requests
import threading
import hashlib
import re
import json
import string
import random
import uuid
import time
import base64
import signal
import urllib.parse
import os
import sys

from flask import Flask, Response, send_file, request
from datetime import timedelta, datetime, timezone
from typing import Any, cast

__all__ = ["Subscription", "BWatch"]

nginx_404 = (
    "<html>\r\n"
    "<head><title>404 Not Found</title></head>\r\n"
    "<body>\r\n"
    "<center><h1>404 Not Found</h1></center>\r\n"
    "<hr><center>nginx/1.29.7</center>\r\n"
    "</body>\r\n"
    "</html>\r\n"
)

if sys.platform != 'linux':
    raise RuntimeError("Must be run on Linux.")
# time.time() - panel._cache_time < panel.refresh_interval
class XUiSession(requests.Session):
    """Internal use only class for making requests to XUi easy.
    Dependencies: none
    Classes depending on this: Subscription"""
    def __init__(self,
                 name: str,
                 address: str,
                 port: int | str,
                 uri: str,
                 username: str,
                 password: str,
                 refresh_interval=60,
                 https: bool = False,
                 nginx_auth: tuple | None = None,  # nginx_auth=('user', 'pass')
                 ignore_inbounds: list[int] | None = None
                ):
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            super().__init__()
            self.username = username
            self.password = password
            self.refresh_interval = refresh_interval
            self.ignore_inbounds = ignore_inbounds if ignore_inbounds is not None else []
            protocol = "https" if https else "http"
            clean_uri = f"/{uri.strip('/')}/" if uri.strip('/') else "/"
            self.port = str(port)
            self.address = address
            self.name = name
            self.local = self.address in ('localhost', '::1', '127.0.0.1')
            self.base_url = f"{protocol}://{address}:{self.port}{clean_uri}"
            self.last_login = None
            self._lock = threading.Lock()
            self._running = False
            self._cache: list[dict] | None = None
            self._cache_time: float = 0
    
            if nginx_auth:
                self.auth = nginx_auth
            self.login()

    def request(self, *args, **kwargs):
        if self._needs_refresh():
            self.login()
        return super().request(*args, **kwargs)

    def login(self):
        self.log.debug(f"{self.address}:{self.port} > logging into 3x-ui ")
        with self._lock:
            try:
                login_url = f"{self.base_url}login"
                login_data = {"username": self.username, "password": self.password}
                response = super().request(
                    "POST",
                    login_url,
                    json=login_data,
                    headers={"Content-Type": "application/json"},
                    timeout=10
                )

                if response.status_code != 200:
                    raise Exception(f"HTTP {response.status_code}")

                json_res = response.json()
                if not json_res.get("success"):
                    raise Exception(f"{json_res.get('msg')}")

                self.last_login = datetime.now()
                self.log.info(f"{self.address}:{self.port} > logged in as {self.username}")

                if not self._running:
                    self._start_refresh_thread()

            except Exception as e:
                self.log.critical(f"{self.address}:{self.port} > login failed: {str(e)}")
                raise

    def _start_refresh_thread(self):
        self._running = True
        def refresh_loop():
            while self._running:
                time.sleep(60)
                if self._needs_refresh():
                    try:
                        self.log.info(f"{self.address}:{self.port} > refreshing session")
                        self.login()
                    except Exception:
                        pass
        thread = threading.Thread(target=refresh_loop, daemon=True)
        thread.start()

    def _needs_refresh(self):
        if not self.last_login:
            return True
        return (datetime.now() - self.last_login) > timedelta(minutes=self.refresh_interval)

class Subscription:
    """Core class, the heart of this monolith.
    Dependencies: XUiSession, Config.
    Classes depending on this: Literally all except Logger, Config, XUiSession"""
    def __init__(self, 
                 cfg: Config, 
                 panels: list[XUiSession],
                 whitelist_panel: XUiSession | None):
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg = cfg
            self.whitelist_panel = whitelist_panel
            self.browser_html = ""
            self.fps = self.cfg['fingerprints']
            self.nginx404 = nginx_404
            self.resp = Response(self.nginx404, status=404, mimetype='text/html')
            self.BROWSER_UA = re.compile(r'(MSIE|Trident|(?!Gecko.+)Firefox|(?!AppleWebKit.+Chrome.+)Safari(?!.+Edge)|(?!AppleWebKit.+)Chrome(?!.+Edge)|(?!AppleWebKit.+Chrome.+Safari.+)Edge|AppleWebKit(?!.+Chrome|.+Safari)|Gecko(?!.+Firefox))(?: |\/)([\d\.apre]+)')
            self.RATIO = 1.073741824 # GiB to GB ratio. constant
            self.FILTERS = {
                'displayname': ':;"\'?/<>{}[]*&^%$#@\\|',
            }
            self.panels = panels.copy()
            self.SALT = self.cfg['salt']
            with open('/var/www/sub/index.html', 'r') as f:
                self.browser_html = f.read()
            if self.whitelist_panel:
                self.panels.append(self.whitelist_panel)

    def hash(self, s: str) -> str:
        return hashlib.sha256((self.SALT + s).encode()).hexdigest()
    
    def isuuid(self, s: str) -> bool:
        """Validate a UUID."""
        try:
            val = uuid.UUID(s, version=4)
            return str(val) == s.lower()
        except ValueError:
            return False
    
    def isbrowser(self, ua: str):
        """Parse a User-Agent against a regex to detect if it is a browser."""
        return self.BROWSER_UA.search(ua)

    def sanitize(self, s: str) -> str:
        """Sanitize an external username"""
        s = s[:32]
        return re.sub(r'[^A-Za-z0-9_\-]', '', s)

    def usertotoken(self, token: str) -> str | None:
        """Get a username from a token, None if doesnt exist."""
        if len(token) < 30:
            return None
        reversed_tokens = {t: u for u, t in self.cfg["tokens"].items()}
        return reversed_tokens.get(token, None)

    def isuser(self, username: str):
        """Know if a username exists."""
        return username and username in self.cfg['users']
    def restart(self, delay: int | float = 0.1) -> None:
        """Restart gunicorn with a delay (in seconds, defaults to 100ms)"""
        def _restart():
            try:
                sig = signal.SIGHUP
            except NameError:
                self.log.critical("Failed to restart. Are you running on Linux?")
                raise
            time.sleep(delay)
            os.kill(os.getppid(), sig)
        threading.Thread(target=_restart, daemon=True).start()
    def getstatus(self, panel: XUiSession) -> dict:
        """Get the information about a panel."""
        try:
            x = panel.get(f"{panel.base_url}panel/api/server/status")
            data = x.json()
            if x.status_code not in [200]:
                self.log.error(f"getstatus fail: {data['msg']}")
                return {}
            return data['obj']
        except Exception as e:
            self.log.error(f"getstatus fail: {e}")
            return {}
    def getinbounds(self, 
                    panel: XUiSession, 
                    cache: bool | None = None) -> list[dict]:
        """Get the inbounds list.
        cache Control the caching. True = return cached data, False = get fresh data (no cache), None = standard cache system"""
        def _fetch() -> list[dict]:
            x = panel.get(f"{panel.base_url}panel/api/inbounds/list")
            data = x.json()
            if x.status_code not in [200]:
                self.log.error(f"getinbounds fail: {data['msg']}")
                return []
            x = data['obj']
            panel._cache = x
            panel._cache_time = time.time()
            return x
        try: 
            inbounds = []
            if cache is not None:
                if cache:
                    inbounds = panel._cache or []
                else:
                    inbounds = _fetch()
            else:             
                if panel.local:
                    inbounds = _fetch()
                else:
                    if time.time() - panel._cache_time < 15:
                        if panel._cache:
                            inbounds = panel._cache
                        else:
                            inbounds = _fetch()
                    else:
                        inbounds = _fetch()
                    
            if panel.ignore_inbounds:
                inbounds = [i for i in inbounds if i['id'] not in panel.ignore_inbounds]
            return inbounds
        except Exception as e:
            self.log.warning(f"getinbounds fail: {e}")
            return []
    def bandwidth(self, username: str, whitelist: bool = False) -> list[int]:
        """Get bandwidth info about a user.
        Output: [upload, download, total]. In bytes."""
        userid = self.cfg["users"][username]
        # whitelist True -> use self.whitelist_panel ONLY
        # whitelist False -> use all except self.whitelist_panel
        panels = [self.whitelist_panel] if whitelist else list(self.panels)
        if not whitelist and self.whitelist_panel: panels.remove(self.whitelist_panel)
        traffic_records = [[], []]
        if not panels:
            return [0, 0, 0]
        for panel in panels:
            inbounds = self.getinbounds(panel) # type: ignore
            for i in inbounds:
                for v in (i['clientStats'] or []):
                    if v['uuid'] == userid:
                        traffic_records[0].append(v['up'])
                        traffic_records[1].append(v['down'])
        
        total = [0, 0, 0]
        total[0] = sum(traffic_records[0])
        total[1] = sum(traffic_records[1])
        total[2] = total[0] + total[1]
        return total
    def add_users(self, username: str) -> None:
        """Add a user to panels. Dont confuse with add_new_user!"""
        userid = self.cfg['users'][username]
        panels = self.panels
        
        for panel in panels:
            inbounds = self.getinbounds(panel)
            list_2_add = []
            need_vision = []
            for i in inbounds:
                if i['protocol'] != "vless":
                    self.log.debug(f"Non-VLESS inbound found ({i['protocol']}). Ignoring.")
                    continue
                already_exists = any(v['uuid'] == userid for v in i['clientStats'])
                if already_exists:
                    continue
                list_2_add.append(i['id'])
                streamsettings = json.loads(i['streamSettings'])
                if streamsettings['network'] in ['tcp', 'raw']:
                    need_vision.append(i['id'])
            
            payload = {"clients": [{
                "id": userid,
                "flow": "",
                "email": "",
                "limitIp": 0,
                "totalGB": 0,
                "expiryTime": 0,
                "enable": True,
                "tgId": "",
                "subId": "",
                "comment": "",
                "reset": 0
            }]}

            for j in list_2_add:
                payload['clients'][0]['email'] = f"{username}-{''.join(random.choices(string.ascii_lowercase + string.digits, k=8))}"
                payload['clients'][0]['flow'] = "xtls-rprx-vision" if j in need_vision else ""
                data = {
                    'id': j,
                    'settings': json.dumps(payload)
                }
                response = panel.post(
                    f"{panel.base_url}panel/api/inbounds/addClient",
                    data=data,
                    headers={'Accept': 'application/json'}
                )
                response_json = response.json()
                if response.status_code in [200, 201] and response_json['success']:
                    self.log.info(f"add user: successfully added {username} to ID {j}")
                else:
                    self.log.error(f"add user: failed to add user {username}:\n{response.text}")
                    raise Exception(response.text)
    def delete_user(self, username: str, perma: bool = False) -> None:
        """Delete a user, either from panels or from storage too."""
        userid = self.cfg['users'][username]
        panels = self.panels

        for panel in panels:
            inbounds = self.getinbounds(panel)

            for j in inbounds:
                k = panel.post(
                    f"{panel.base_url}panel/api/inbounds/{str(j['id'])}/delClient/{userid}",
                    headers={'Accept': 'application/json'}
                )


        if perma:
            with self.cfg as data:
                sections = ['users', 'tokens', 'userFingerprints', 'status', 'statusWl', 'displaynames', 'bw', 'wl_bw', 'statusTime', 'time']
                for s in sections:
                    data.get(s, {}).pop(username, None)
                
                emails_to_delete =[email for email, uname in data.get('webui_users', {}).items() if uname == username]
                for email in emails_to_delete:
                    data.get('webui_users', {}).pop(email, None)
                    data.get('webui_passwords', {}).pop(email, None)
                
                tgids_to_delete =[tgid for tgid, uname in data.get('tgids', {}).items() if uname == username]
                for tgid in tgids_to_delete:
                    data.get('tgids', {}).pop(tgid, None)

            self.restart()
        self.log.info(f"deleted user {username} {"permanently" if perma else ""}")
    def update_user(self, 
                    username: str, 
                    enable: bool | None = None, 
                    timee: bool | None = None, 
                    wl_enable: bool | None = None) -> None:
        """Disable/enable a user. wl_enable controls specifically the whitelist node."""
        userid = self.cfg['users'][username]
        if enable is not None:
            panels = list(self.panels)
            if self.whitelist_panel: panels.remove(self.whitelist_panel)
            for panel in panels:
                # if panel == self.whitelist_panel: continue 
                inbounds = self.getinbounds(panel)
                l = [i['id'] for i in inbounds if i['protocol'] == "vless"]
                the = {str(vi['id']): vx for vi in inbounds for vx in vi['clientStats'] if vx['uuid'] == userid}
                for k in l:
                    if str(k) not in the: continue
                    payload = the[str(k)]
                    payload['enable'] = enable
                    del payload['inboundId']
                    payload['id'] = userid
                    panel.post(
                        f"{panel.base_url}panel/api/inbounds/updateClient/{userid}",
                        data={'id': k, 'settings': json.dumps({"clients": [payload]})},
                        headers={'Accept': 'application/json'}
                    )
            
            with self.cfg as data:
                data['status'][username] = enable
                if timee is not None: data['statusTime'][username] = timee
            
            self.log.info(f"Set main status for {username} to {enable}")

        if wl_enable is not None and self.whitelist_panel:
            panel = self.whitelist_panel
            inbounds = self.getinbounds(panel)
            l = [i['id'] for i in inbounds if i['protocol'] == "vless"]
            the = {str(vi['id']): vx for vi in inbounds for vx in vi['clientStats'] if vx['uuid'] == userid}
            
            for k in l:
                if str(k) not in the: continue
                payload = the[str(k)]
                payload['enable'] = wl_enable
                del payload['inboundId']
                payload['id'] = userid
                
                panel.post(
                    f"{panel.base_url}panel/api/inbounds/updateClient/{userid}",
                    data={'id': k, 'settings': json.dumps({"clients": [payload]})},
                    headers={'Accept': 'application/json'}
                )
                
            with self.cfg as data:
                data.setdefault('statusWl', {})[username] = wl_enable
            
            self.log.info(f"Set WL status for {username} to {wl_enable}")
    def add_new_user(
        self,
        username: str,
        displayname: str,
        ext_username: str | None = None,
        ext_password: str | None = None,
        token: str | None = None,
        userid: str | None = None,
        fingerprint: str | None = None,
        limit: int = 0,
        wl_limit: int = 5,
        timee: int = 0,
        restart: bool = True
    ) -> dict | str:
        """Adds a new user. Returns string with error if any argument is incorrect.
        Now also suppports ext username and password (optional). Sanitize them first!"""
        if ext_username:
            ext_username = self.sanitize(ext_username)
            if len(ext_username) > 32:
                return "Ext Username too long"
        if token is None:
            token = ''.join(random.choices(string.ascii_letters + string.digits + '_-', k=40))
        if userid is None:
            userid = str(uuid.uuid4())
        else:
            if not self.isuuid(userid):
                return "Invalid UUID"
        if fingerprint is None:
            fingerprint = random.choice(self.fps)
        else:
            if fingerprint not in self.cfg['fingerprints']:
                return "Invalid fingerprint"
        if timee > 2**31:
            return "Invalid timestamp"
        if ext_password is not None:
            ext_password = self.hash(ext_password)


        displayname = displayname.translate(str.maketrans('', '', self.FILTERS['displayname']))
        if len(displayname) > 16:
            return "Displayname too long"
        with self.cfg as d:
            # Atomic uniqueness checks under the batch lock (prevents races)
            if username in d['users']:
                return "Username exists"
            if ext_username and ext_username in d.get('webui_users', {}):
                return "Ext Username exists"
            d['users'][username] = userid
            d['tokens'][username] = token
            d['userFingerprints'][username] = fingerprint
            d['status'][username] = True
            d['statusTime'][username] = True
            d['statusWl'][username] = True
            d['displaynames'][username] = displayname
            d['bw'][username] = [limit, 0]
            d['wl_bw'][username] = [wl_limit, 0]
            d['time'][username] = timee
            if ext_password and ext_username:
                d.setdefault('webui_passwords', {})[ext_username] = ext_password
                d.setdefault('webui_users', {})[ext_username] = username
        
        try: self.add_users(username=username)
        except Exception as e: raise Exception(f"add new user: {username}: {e}")
        if restart: self.restart()
        return {"username": username, "token": token, "uuid": userid, "fingerprint": fingerprint, "displayname": displayname} # for API
    def update_params(
        self,
        username: str,
        displayname: str | None = None,
        ext_username: str | None = None,
        ext_password: str | None = None,
        token: str | None = None,
        fingerprint: str | None = None,
        limit: int | None = None,
        wl_limit: int | None = None,
        timee: int | None = None,
        restart: bool = True
    ) -> str | None:
        """Updates certain fields for any user. Changing UUIDs isnt supported.
        Invalid params return str, None on success."""
        if not self.isuser(username):
            return "Missing username"
        _pw_already_hashed = False
        _old_ext_username = None
        if ext_username is not None:
            
            for _ext, _internal in self.cfg.get('webui_users', {}).items():
                if _internal == username:
                    _old_ext_username = _ext
                    break
            if ext_password is None and _old_ext_username:
                ext_password = self.cfg['webui_passwords'].get(_old_ext_username)
                _pw_already_hashed = True
                    
        if displayname is None:
            displayname = self.cfg['displaynames'][username]
        else:
            displayname = displayname.translate(str.maketrans('', '', self.FILTERS['displayname']))
            if len(displayname) > 16:
                return "Displayname too long"
        if token is None:
            token = self.cfg['tokens'][username]
        if fingerprint is None:
            fingerprint = self.cfg['userFingerprints'][username]
        if limit is None:
            limit = self.cfg['bw'][username][0]
        if wl_limit is None:
            wl_limit = self.cfg['wl_bw'][username][0]
        if timee is None:
            timee = self.cfg['time'][username]
        if ext_password is not None and not _pw_already_hashed:
            ext_password = self.hash(ext_password)
        if ext_username is not None:
            ext_username = self.sanitize(ext_username)
            if len(ext_username) > 32:
                return "Username too long"
            if ext_username in self.cfg['webui_users'] and self.cfg['webui_users'][ext_username] != username:
                return "Ext username exists"
            
        if fingerprint not in self.cfg['fingerprints']:
            return "Invalid fingerprint"
        if timee > 2**31: # type: ignore # this is a useless error
            return "Invalid time"

        with self.cfg as t:
            t['displaynames'][username] =  displayname
            t['tokens'][username] = token
            t['userFingerprints'][username] = fingerprint
            t['bw'][username] = [limit, self.cfg['bw'][username][1]]
            t['wl_bw'][username] = [wl_limit, self.cfg['wl_bw'][username][1]]
            t['time'][username] = timee
            if ext_password and ext_username:
                if _old_ext_username and _old_ext_username != ext_username:
                    t.get('webui_passwords', {}).pop(_old_ext_username, None)
                    t.get('webui_users', {}).pop(_old_ext_username, None)
                t['webui_passwords'][ext_username] = ext_password
                t['webui_users'][ext_username] = username                

        if restart: self.restart()
    def update_uuid(self, username: str, uid: str) -> bool:
        """Seperate method for updating the UUID. True on success.
        Potentially dangerous operation, seperate function."""
        if not self.isuser(username):
            return False
        olduid = self.cfg['users'][username]
        for panel in self.panels:
            inbounds = self.getinbounds(panel)
            l = []
            need_vision = []
            for i in inbounds:
                l.append(i['id'])
            the = {}
            for vi in inbounds:
                for vx in vi['clientStats']:
                    if vx['uuid'] == olduid:
                        the[str(vi['id'])] = vx
                        if json.loads(vi['streamSettings'])['network'] in ["tcp", "raw"]:
                            need_vision.append(vi['id'])
                        break
            for k in l:
                if str(k) not in the:
                    continue
                payload = the[str(k)]
                payload['id'] = uid
                payload['flow'] = "xtls-rprx-vision" if k in need_vision else ""
                del payload['inboundId']
                data = {
                    'id': k,
                    'settings': json.dumps({"clients": [payload]})
                }
                response = panel.post(
                    f"{panel.base_url}panel/api/inbounds/updateClient/{olduid}",
                    data=data,
                    headers={'Accept': 'application/json'}
                )
                if not (response.status_code in [200, 201] and response.json().get('success')):
                    self.log.critical(f"update_uuid failed: {response.json().get('msg')}")
        with self.cfg as t: t['users'][username] = uid

        return True
    def get_code(self, code: str) -> dict | bool:
        """Search for a code. Returns a dict if found, False if isnt."""

        result = next((item for item in self.cfg['codes'] if item.get("code") == code), None)

        if result:
            code_type = result.get('action', '')
            if code_type not in ["register", "bonus"]:
                self.log.critical(f"code {code} has an action {code_type}, must be either register or bonus")
                raise ValueError("")
            return {"action": code_type, 
                    "perma": result.get('perma', False), 
                    "days": result.get('days', 0), 
                    "gb": result.get('gb', 0), 
                    "wl_gb": result.get('wl_gb', 0)}
        else:
            return False
    def add_code(self, 
                 code: str, 
                 action: str, 
                 permanent: bool = False, 
                 days: int = 0, 
                 gb: int = 0,
                 wl_gb: int = 0) -> None:
        """Creates a code. Simple."""

        if action not in ["register", "bonus"]:
            self.log.critical(f"failed to create code {code}: invalid action {action}")
            raise ValueError(f"code {code} action must be register or bonus")

        with self.cfg as t:
            t['codes'].append({"code": code, "action": action, "perma": permanent, "days": days, "gb": gb, "wl_gb": wl_gb})

    def consume_code(self, code: str) -> dict | bool:
        """Atomically find, validate, and delete (if non-perma) a code.
        Returns the code dict on success, False if not found or invalid."""
        box = []
        def _u(t):
            for i, item in enumerate(t['codes']):
                if item.get('code') == code:
                    action = item.get('action', '')
                    if action not in ['register', 'bonus']:
                        return
                    box.append({
                        'action': action,
                        'perma': item.get('perma', False),
                        'days': item.get('days', 0),
                        'gb': item.get('gb', 0),
                        'wl_gb': item.get('wl_gb', 0),
                    })
                    if not item.get('perma', False):
                        del t['codes'][i]
                    return
        self.cfg.update(_u)
        return box[0] if box else False

    def delete_code(self, code: str) -> None | bool:
        """Deletes a specified code. False if not found. Ignores 'perma' flag obviously."""
        
        pos = None
        for i, item in enumerate(self.cfg['codes']):
            if item.get('code') == code:
                pos = i
                break
        
        if pos is None:
            return False
            
        def _u(t):
            del t['codes'][pos]
        self.cfg.update(_u)
    def list_code(self) -> list:
        """List all codes, without any additional info."""
        x = []
        for i in self.cfg['codes']:
            x.append(i.get('code'))
        return x
    def bonus_code(self, value: int | str, code: str, telegram: bool = True) -> None | bool:
        """Apply a bonus code to a user from Telegram. False if code not valid.
        telegram: if True, provide a ID for value. if False, provide internal username"""

        userobj = self.get_info_telegram(tgid=value, reverse=not telegram)
        if not userobj:
            return False
        codeobj = self.consume_code(code=code)
        if not isinstance(codeobj, dict):
            return False
        if codeobj['action'] != "bonus": # Only bonus codes allowed
            return False

        username = userobj['username']
        days = codeobj.get('days', 0)
        ts = userobj['time']
        if ts: # No time limit -> keep unchanged
            timee = int(ts + (days * 86400)) if days else None # No increment -> dont change
        else:
            timee = None
        limit = codeobj.get('gb', 0)
        wl_limit = codeobj.get('wl_gb', 0)
        ul = userobj['limit']
        wl_ul = userobj['wl_limit']
        if ul:
            limit = ul + limit if limit else None
        else:
            limit = None
        if wl_ul:
            wl_limit = wl_ul + wl_limit if wl_limit else None
        else:
            wl_limit = None
        self.update_params(
            username=username,
            limit=limit,
            wl_limit=wl_limit,
            timee=timee
        )
    def get_info_telegram(self, tgid: int | str, reverse: bool = False) -> dict | None:
        """Returns all user info by telegram ID. None if target uid wasnt found.
        reverse: lookup by internal usermame"""
        if reverse:
            username = tgid
        else:
            username = cast(str, self.cfg['tgids'].get(str(tgid)))
        if not username:
            return None
        bandwidths = self.bandwidth(username=username) # type: ignore # this useless too btw i guarded everything
        wl_bandwidths = self.bandwidth(username=username, whitelist=True) # type: ignore
        return {
            "username": username,
            "up": round(bandwidths[0] / 10**6, 2),
            "down": round(bandwidths[1] / 10**6, 2),
            "wl_up": round(wl_bandwidths[0] / 10**6, 2),
            "wl_down": round(wl_bandwidths[1] / 10**6, 2),
            "total": round(bandwidths[2] / 10**6, 2),
            "wl_total": round(wl_bandwidths[2] / 10**6, 2),
            "monthly": round(self.cfg['bw'][username][1] / 10**6),
            "limit": self.cfg['bw'][username][0],
            "wl_monthly": round(self.cfg['wl_bw'][username][1] / 10**6),
            "wl_limit": self.cfg['wl_bw'][username][0],
            "time": self.cfg['time'].get(username, 0),
            "token": self.cfg['tokens'].get(username),
            "displayname": self.cfg['displaynames'].get(username),
            "status": self.cfg['status'].get(username)
        }
    def is_registered(self, tgid: int) -> bool:
        """Check if a telegram user is already registered."""
        return str(tgid) in self.cfg['tgids']
    def get_username_telegram(self, tgid: int | str, reverse: bool = False) -> int | str | None:
        """Get the internal username for a tgid.
        Parameter reverse: if True, get tg id from username. Otherwise default behaviour."""
        if not reverse:
            return self.cfg['tgids'].get(str(tgid))
        for i, v in self.cfg['tgids'].items():
            if v == tgid:
                return int(i)
                
    def get_emails(self, username: str, panel: XUiSession) -> dict[str, str]:
        """Get the panel emails. {'inboundId': 'actual_panel_email', ...}"""
        inbounds = self.getinbounds(panel=panel)
        emails = {}
        for i in inbounds:
            for r in i['clientStats']:
                actual_email = r.get('email', '')
                parts = actual_email.rsplit('-', 1)
                parsed_username = parts[0] if len(parts) == 2 else actual_email
                if parsed_username == username:
                    emails[str(i['id'])] = actual_email
        return emails

    def get_online_users(self, new: bool = False) -> list[str] | dict[str, str | None]:
        """Get the list of currently online users.
        new: False = ['username', ...]. True = {'username': ext_username_or_None, ...}"""
        online_users = set()
        for panel in self.panels:
            try:
                res = panel.post(f"{panel.base_url}panel/api/inbounds/onlines")
                if res.status_code in [200, 201] and res.json().get('success'):
                    for email in res.json().get('obj', []):
                        name_candidate = email.rsplit('-', 1)[0]
                        if name_candidate in self.cfg['users']:
                            online_users.add(name_candidate)
            except Exception as e:
                self.log.error(f"Online check error: {e}")
        
        if not new:
            return list(online_users)
        
        internal_to_ext = {v: k for k, v in self.cfg.get('webui_users', {}).items()}
        return {name: internal_to_ext.get(name) for name in online_users}
    def is_online(self, username: str) -> bool:
        """Simplest method here lol. But useful."""
        return username in self.get_online_users()
    def reset_user(self, username: str) -> dict:
        """Resets token and uuid to randomness. Dict with new values on success."""
        if not self.isuser(username):
            self.log.warning(f"reset_user: user {username} not found!")
            raise Exception
        newid = str(uuid.uuid4())
        newt = ''.join(random.choices(string.ascii_lowercase + string.ascii_uppercase + string.digits, k=40))
        if not self.update_uuid(username, newid):
            self.log.critical("reset_user: something happened, look above")
            raise Exception
        if self.update_params(
            username=username,
            token=newt,
            restart=False
        ) != None:
            self.log.critical(f"reset_user: something happened")
            raise Exception
        self.restart(0.2)
        return {'uuid': newid, 'token': newt}
    def fmt_bytes(self, value: int) -> tuple[str, str]:
        for unit, div in (("TB", 10**12), ("GB", 10**9), ("MB", 10**6)):
            if value >= div:
                return str(round(value / div, 2)), unit
        return str(round(value / 10**6, 2)), "MB"
    def fmt_time(self, seconds: int) -> str:
        if seconds < 60:
            return f"{seconds}с"
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        d, h = divmod(h, 24)
        if d > 0:
            return f"{d}д {h}ч {m}м"
        if h > 0:
            return f"{h}ч {m}м"
        return f"{m}м"
    def get_subscription(self, token: str, lang: str, ua: str, ip: str) -> Response:
        """You alredy know what this is."""
        if not token:
            return self.resp
        username = self.usertotoken(token)
        self.log.info(f"""subscription hit!
IP: {ip}
User-Agent: {ua}
Token: {token}
Username: {"none" if not username else username}
Lang: {lang}""")
        if not username or username not in self.cfg['users']:
            return self.resp
        if lang not in ["ru", "en"]:
            return self.resp
        bandwidths = self.bandwidth(username)

        browser = self.isbrowser(ua=ua)
        name = self.cfg['displaynames'][username]
        need_dummy_link = True if ("v2rayNG" in ua) or ("v2rayN" in ua) else False
        is_happ = True if ua.startswith("Happ/") else False

        status = self.cfg['status'][username]
        statusTime = self.cfg['statusTime'][username]
        statusWl = self.cfg['statusWl'][username]
        times = self.cfg['time'][username]
        mimetype = "text/plain" if not browser else "text/html"
        sub_name = self.cfg['sub_name']
        userinfo = "upload=%i1;download=%i2;total=%i3;expire=%i4"
        if browser:
            return Response(self.browser_html, mimetype=mimetype)

        # desc = desc.replace("%s3", name)
        desc = self.build_description(
            username=username,
            name=name,
            lang=lang,
            bandwidths=bandwidths,
            status=status,
            statusWl=statusWl,
            statusTime=statusTime,
            ts=times
        )
        announce = f"base64:{base64.b64encode(desc.encode('utf-8')).decode('utf-8')}"
        if status:
            userinfo = userinfo.replace("%i1", str(bandwidths[0]) if self.cfg['bw'][username][0] == 0 else str(int(self.cfg['bw'][username][1] / 2 * self.RATIO)))
            userinfo = userinfo.replace("%i2", str(bandwidths[1]) if self.cfg['bw'][username][0] == 0 else str(int(self.cfg['bw'][username][1] / 2 * self.RATIO)))
            userinfo = userinfo.replace("%i3", "0" if self.cfg['bw'][username][0] == 0 else str(int(self.cfg['bw'][username][0] * 10**9 * self.RATIO)))
        else:
            userinfo = userinfo.replace("%i1", str(int(self.cfg['bw'][username][0] * 10**9 / 2 * self.RATIO)))
            userinfo = userinfo.replace("%i2", str(int(self.cfg['bw'][username][0] * 10**9 / 2 * self.RATIO)))
            userinfo = userinfo.replace("%i3", str(int(self.cfg['bw'][username][0] * 10**9 * self.RATIO)))
        userinfo = userinfo.replace("%i4", str(times))
        headers = {
            'Profile-Title': sub_name,
            'Subscription-Userinfo': userinfo,
            'profile-update-interval': "1",
            # funny
            'x-stop-looking-here': 'please',
            'x-pomidorka': '1515',
            'announce': announce,
            'Content-Type': "text/plain"
        }
        if is_happ: headers['routing'] = f"happ://routing/onadd/{base64.b64encode(json.dumps(self.cfg['routing']).encode('utf-8')).decode('utf-8')}"
        user_uuid = self.cfg['users'][username]
        generated_links = []

        for p_key, p_name in self.cfg['profiles'].items():
            if p_key in self.cfg['whitelistProfiles'] and not statusWl:
                continue
            if not status:
                break
            link = self.cfg['masterLinks'][p_key]
            flag = self.cfg['flags'][p_key]
            node = self.cfg['profileNodes'][p_key]
            domain = self.cfg['nodes'][node]
            if not is_happ:
                flag = ""
            link = link.replace("DOMAIN", domain)
            if "FINGERPRINT" in link: link = link.replace("FINGERPRINT", self.cfg['userFingerprints'][username])
            link = link.replace("UUID", user_uuid)
            link = link.replace("NAME", (flag + p_name[0] if lang == "en" else flag + p_name[1]))
            if "EXTRA" in link:
                extra_data = self.cfg['xhttpExtra'].get(p_key)
                if extra_data:
                    json_str = json.dumps(extra_data, separators=(',', ':'))
                    encoded_extra = urllib.parse.quote(json_str)
                    link = link.replace("EXTRA", encoded_extra)
                else:
                    link = link.replace("extra=EXTRA&", "").replace("&extra=EXTRA", "").replace("extra=EXTRA", "")
            generated_links.append(link)
        dt = "Bandwidth: " if lang == "en" else "Трафик: "
        dt = dt + "↑ %s1%u1 / ↓ %s2%u2"
        if need_dummy_link:
            if (int(bandwidths[0]) / 10**6) > 10**4:
                dt = dt.replace("%s1", str(round(bandwidths[0] / 10**9, 1)))
                dt = dt.replace("%u1", "GB")
            else:
                dt = dt.replace("%s1", str(int(round(bandwidths[0] / 10**6, 0))))
                dt = dt.replace("%u1", "MB")
            if (int(bandwidths[1]) / 10**6) > 10**4:
                dt = dt.replace("%s2", str(round(bandwidths[1] / 10**9, 1)))
                dt = dt.replace("%u2", "GB")
            else:
                dt = dt.replace("%s2", str(int(round(bandwidths[1] / 10**6, 0))))
                dt = dt.replace("%u2", "MB")

        dt = urllib.parse.quote(dt)
        dummy = f"vless://0@localhost:1?type=tcp&security=none#" + dt
        if need_dummy_link:
            generated_links = [dummy] + generated_links
        raw_text = "\n".join(generated_links)
        payload = base64.b64encode(raw_text.encode('utf-8')).decode('utf-8')

        return Response(payload, mimetype=mimetype, headers=headers)
    def build_description(self, 
                          username: str,
                          name: str,
                          lang: str,
                          bandwidths: list[int], 
                          status: bool,
                          statusWl: bool,
                          statusTime: bool,
                          ts: int) -> str:

        desc = self.cfg['description'][0] if lang == "en" else self.cfg['description'][1]
        if not status:
            desc = self.cfg['description'][2] if lang == "en" else self.cfg['description'][3]
        if status:
            v, label = self.fmt_bytes(int(bandwidths[0]))
            desc = desc.replace("%s1", v).replace("%u1", label)
            v, label = self.fmt_bytes(int(bandwidths[1]))
            desc = desc.replace("%s2", v).replace("%u2", label)

            if self.cfg['bw'][username][0] != 0:
                desc = desc.replace("%x1", self.cfg['description'][4] if lang == "en" else self.cfg['description'][5])
            else:
                desc = desc.replace("%x1", "")
            if self.cfg['time'][username] != 0:
                desc = desc.replace("%t1", self.cfg['description'][6] if lang == "en" else self.cfg['description'][7])
                desc = desc.replace(
                    "%t3",
                    datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=3))).strftime("%d.%m.%y %H:%M")
                )
                desc = desc.replace(
                    "%t2",
                    str((ts - int(time.time())) // 86400)
                )
            else:
                t1 = (self.cfg['description'][4] if lang == "en" else self.cfg['description'][5]) if self.cfg['bw'][username][0] != 0 else ""
                desc = desc.replace("%t1", t1).replace("%t2", "").replace("%t3", "")
            if self.cfg['bw'][username][0] != 0:
                v, label = self.fmt_bytes(int(self.cfg['bw'][username][1]))
                desc = desc.replace("%n1", v).replace("%y1", label)
                desc = desc.replace("%n2", str(self.cfg['bw'][username][0])).replace("%y2", "GB")
            else:
                desc = desc.replace("%n1", "").replace("%y1", "").replace("%n2", "").replace("%y2", "")
            if self.cfg['wl_bw'][username][0] != 0:
                if self.cfg['wl_bw'][username][1] > self.cfg['wl_bw'][username][0] * 10**9: 
                    desc = desc.replace("%l1", self.cfg['description'][12] if lang == "en" else self.cfg['description'][13])
                else:
                    desc = desc.replace("%l1", self.cfg['description'][10] if lang == "en" else self.cfg['description'][11])
                
                v, label = self.fmt_bytes(int(self.cfg['wl_bw'][username][1]))
                desc = desc.replace("%w1", v).replace("%i1", label)

                desc = desc.replace("%w2", str(self.cfg['wl_bw'][username][0])).replace("%i2", "GB")
            else:
                desc = desc.replace("%l1", "").replace("%w1", "").replace("%i1", "").replace("%w2", "").replace("%i2", "")
        else:
            if self.cfg['bw'][username][0] != 0:
                v, label = self.fmt_bytes(int(self.cfg['bw'][username][1]))
                desc = desc.replace("%n1", v).replace("%u1", label)

                desc = desc.replace("%n2", str(self.cfg['bw'][username][0])).replace("%u2", "GB")
            if not statusTime:
                desc = desc.replace("%t1", self.cfg['description'][8] if lang == "en" else self.cfg['description'][9])
                desc = desc.replace(
                    "%t3",
                    datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=3))).strftime("%d.%m.%y %H:%M")
                )
                desc = desc.replace(
                    "%t2",
                    str(-(ts - int(time.time())) // 86400)
                )
            else:
                desc = desc.replace("%t1", "").replace("%t2", "").replace("%t3", "")
        desc = desc.replace("%s3", name)
        return desc

class BWatch:
    """Class for monitoring bandwidth.
    Dependencies: Subscription
    Classes depending on this: Api, WebApi"""
    def __init__(self, 
                 cfg: Config, 
                 sub: Subscription, 
                 bot: Any | None = None):
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg = cfg
            self._lock = threading.Lock()
            self._stop_event = threading.Event()
            self.sub = sub
            self.bot = bot
            self.mem = {}
            self.wl_mem = {}
            self._t1 = threading.Thread(target=self._every_120s, daemon=True)
            self._t2 = threading.Thread(target=self._every_2h, daemon=True)
            self._t3 = threading.Thread(target=self._every_15s, daemon=True)
            self._t4 = threading.Thread(target=self._every_24h, daemon=True)
    
    def start(self):
        for i in list(self.cfg['users'].keys()):
            self.wl_mem[i] = self.sub.bandwidth(username=i, whitelist=True)
            if self.cfg['bw'][i][0] == 0:
                continue
            self.mem[i] = self.sub.bandwidth(username=i)

        self._t1.start()
        self._t2.start()
        self._t3.start()
        self._t4.start()
        self.log.info("loaded BWatch")
    def stop(self):
        self._stop_event.set()

    def bandwidth_check(self):
        updates = {}    # username -> (delta, current) for main
        wl_updates = {} # username -> (delta, current) for whitelist
        for i in list(self.cfg['users'].keys()):
            # Main bandwidth
            if self.cfg['time'].get(i, 0) != 0:
                if (self.cfg['time'][i] - int(time.time())) >= 0 and not self.cfg['statusTime'][i]:
                    try: self.sub.update_user(username=i, enable=True, timee=True)
                    except Exception as e: self.log.critical(f"bw_check: {i}: {e}")
            if self.cfg['bw'].get(i, [0, 0])[0] != 0:
                if self.cfg['bw'][i][1] < self.cfg['bw'][i][0] * 1000**3 and not self.cfg['status'][i]:
                    try: self.sub.update_user(username=i, enable=True)
                    except Exception as e: self.log.critical(f"bw_check: {i}: {e}")
                try:
                    current_bws = self.sub.bandwidth(username=i)
                    if i not in self.mem:
                        self.mem[i] = current_bws
                    else:
                        delta = current_bws[2] - self.mem[i][2]
                        if delta > 0:
                            updates[i] = (delta, current_bws)
                except Exception as e:
                    self.log.critical(f"BW error for {i}: {e}")
            # Whitelist bandwidth
            if self.cfg['wl_bw'].get(i, [0, 0])[0] != 0:
                if self.cfg['wl_bw'][i][1] < self.cfg['wl_bw'][i][0] * 1000**3 and not self.cfg['statusWl'][i]:
                    try: self.sub.update_user(username=i, wl_enable=True)
                    except Exception as e: self.log.critical(f"bw_check: {i}: {e}")
                try:
                    current_bws = self.sub.bandwidth(username=i, whitelist=True)
                    if i not in self.wl_mem:
                        self.wl_mem[i] = current_bws
                    else:
                        delta = current_bws[2] - self.wl_mem[i][2]
                        if delta > 0:
                            wl_updates[i] = (delta, current_bws)
                except Exception as e:
                    self.log.critical(f"BW wl error for {i}: {e}")
        if not updates and not wl_updates:
            return

        with self.cfg as data:
            for i, (delta, cur) in updates.items():
                if i in data['bw']:
                    data['bw'][i][1] += delta
                    self.mem[i] = cur
            for i, (delta, cur) in wl_updates.items():
                if i in data['wl_bw']:
                    data['wl_bw'][i][1] += delta
                    self.wl_mem[i] = cur

    def check(self):
        #1. check if someones limit is over
        # theres no global command cuz we dont dirrmectly edit T <-- these comments so ancient 
        for i in list(self.cfg['users'].keys()):
            if self.cfg['time'][i] != 0:
                if (self.cfg['time'][i] - int(time.time())) <= 0:
                    if self.cfg['statusTime'][i]:
                        try: self.sub.update_user(username=i, enable=False, timee=False)
                        except Exception as e: self.log.critical(f"check: tick: time upd: {str(e)}")
                        if self.bot: self.bot.msg(self.sub.get_username_telegram(tgid=i, reverse=True), 'warning_disabled') # sub expired
                    continue
                else:
                    days = (self.cfg['time'][i] - int(time.time())) // 86400
                    if days <= 2:
                        id = self.sub.get_username_telegram(tgid=i, reverse=True)
                        if id not in self.cfg['_notified']:
                            def _u(t): t['_notified'].append(id)
                            self.cfg.update(_u)
                            if self.bot: self.bot.msg(cast(int, id), 'warning_days', days=days)
            if self.cfg['wl_bw'][i][0] != 0 and self.cfg['wl_bw'][i][1] > self.cfg['wl_bw'][i][0] * 1000**3:
                if self.cfg['statusWl'].get(i, True):
                    try: self.sub.update_user(username=i, wl_enable=False)
                    except Exception as e: self.log.critical(f"check: tick: disable whitelist: {str(e)}")
                    if self.bot: self.bot.msg(self.sub.get_username_telegram(tgid=i, reverse=True), 'warning_traffic_whitelist_disabled', available=self.cfg['wl_bw'][i][0])
            elif self.cfg['wl_bw'][i][0] != 0 and self.cfg['wl_bw'][i][1] > self.cfg['wl_bw'][i][0] * 1000**3 * 0.95: # 95%
                id = self.sub.get_username_telegram(tgid=i, reverse=True)
                if id not in self.cfg['_wl_notified']:
                    def _u(t): t['_wl_notified'].append(id)
                    self.cfg.update(_u)
                    if self.bot: self.bot.msg(id, 'warning_traffic_whitelist', used=int(round(self.cfg['wl_bw'][i][1] / 10**6, 0)), available=self.cfg['wl_bw'][i][0])
            if not self.cfg['status'][i]:
                continue
            if self.cfg['bw'][i][0] == 0:
                continue
            if self.cfg['bw'][i][0] != 0 and self.cfg['bw'][i][1] > self.cfg['bw'][i][0] * 1000**3:
                try: self.sub.update_user(username=i, enable=False, timee=True)
                except Exception as e: self.log.critical(f"check: tick: disable: {str(e)}")
                if self.bot: self.bot.msg(self.sub.get_username_telegram(tgid=i, reverse=True), 'warning_traffic_disabled', available=self.cfg['bw'][i][0])
            elif self.cfg['bw'][i][0] != 0 and self.cfg['bw'][i][1] > self.cfg['bw'][i][0] * 1000**3 * 0.95: # 95%
                id = self.sub.get_username_telegram(tgid=i, reverse=True)
                if id not in self.cfg['_notified']:
                    def _u(t): t['_notified'].append(id)
                    self.cfg.update(_u)                    
                    if self.bot: self.bot.msg(id, 'warning_traffic', used=int(round(self.cfg['bw'][i][1] / 10**6, 0)), available=self.cfg['bw'][i][0])
    def is_first(self):
        now = datetime.now()
        if now.day != 1:
            return
        today = now.strftime("%Y-%m-%d")
        if self.cfg.get("_last_reset") == today:
            return

        with self.cfg as t:
            for k in t['bw']:
                t['bw'][k][1] = 0
            for k in t['wl_bw']:
                t['wl_bw'][k][1] = 0
            t['_last_reset'] = today
            t['_notified'] = []
            t['_wl_notified'] = []

    def reset(self):
        def _u(t): 
            t['_notified'] = []
            t['_wl_notified'] = []
        self.cfg.update(_u)
    def _every_120s(self):
        while not self._stop_event.wait(120):
            self.check()
    def _every_2h(self):
        while not self._stop_event.wait(7200):
            self.is_first()
    def _every_15s(self):
        while not self._stop_event.wait(15):
            self.bandwidth_check()
    def _every_24h(self):
        while not self._stop_event.wait(86400):
            self.reset()
