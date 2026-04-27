from __future__ import annotations

from loggers import Logger
from config import Config
from session import XUiSession

import threading
import hashlib
import hmac
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
import io
import qrcode

from flask import Flask, Response, request
from datetime import timedelta, datetime, timezone
from typing import Any, cast, NamedTuple, overload, Literal
from custom_types import (
    ServerMetricsResponse, Inbound, 
    SettingsClient, NewUserInfo,
    RegisterWithCodeInfo, CodeObject,
    UserInfo, ResetUserObject,
    ApplyBonusCodeObject,
    PublicBotLike, AdminBotLike,
    client_stats_to_settings
)
from dataclasses import dataclass, asdict
from collections.abc import MutableMapping

__all__ = [
    "Subscription", "BWatch", 
    "BandwidthInfo", "BandwidthSnapshot", 
    "fmt_bytes", "fmt_bytes_tuple",
    "SERVER_TZ"
]

nginx_404 = (
    "<html>\r\n"
    "<head><title>404 Not Found</title></head>\r\n"
    "<body>\r\n"
    "<center><h1>404 Not Found</h1></center>\r\n"
    "<hr><center>nginx/1.29.8</center>\r\n"
    "</body>\r\n"
    "</html>\r\n"
)

if sys.platform != 'linux':
    raise RuntimeError("Must be run on Linux.")

SERVER_TZ = timezone(timedelta(hours=3)) # MSK

def fmt_bytes_tuple(value: int | float) -> tuple[str, str]:
    """
    Format bytes into a tuple.
    Returns:
        tuple[amount, label]  
        Example: tuple["193", "MB"]
    """
    for unit, div in (("TB", 10**12), ("GB", 10**9), ("MB", 10**6)):
        if value >= div:
            return str(round(value / div, 2)), unit
    return str(round(value / 10**6, 2)), "MB"

def fmt_bytes(b: float) -> str:
    """Format bytes as short human-readable string. Does NOT return a tuple."""
    if b <= 0:
        return '0'
    for unit, div in (('TB', 10**12), ('GB', 10**9), ('MB', 10**6), ('KB', 10**3)):
        if b >= div:
            return f'{b / div:.1f} {unit}'
    return f'{int(b)} B'

class BandwidthInfo(NamedTuple):
    """
    Every value is in bytes.
    ' | float' is present because get_info formats these values into floats.
    bandwidth() itself returns int, always.
    """
    upload: int | float
    download: int | float
    total: int | float

    @staticmethod
    def format_bytes(b: int) -> float:
        return round(b / 10**6, 2)

    def format_all(self) -> BandwidthInfo:
        return BandwidthInfo(
            self.format_bytes(int(self.upload)),
            self.format_bytes(int(self.download)),
            self.format_bytes(int(self.total))
        )

class BandwidthUpdate(NamedTuple):
    delta: int
    current: BandwidthInfo

@dataclass
class BandwidthSnapshot:
    ts: int
    up: int = 0
    down: int = 0
    wl_up: int = 0
    wl_down: int = 0

class Subscription:
    """Core class.  
    Dependencies: XUiSession, Config.  
    Classes depending on this: Literally all except Logger, Config, XUiSession"""
    def __init__(self, 
                 cfg: Config,
                 bw_cfg: Config,
                 app: Flask,
                 panels: list[XUiSession],
                 whitelist_panel: XUiSession | None):
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg: Config = cfg
            self.bw_cfg: Config = bw_cfg
            self.app: Flask = app
            self.whitelist_panel: XUiSession | None  = whitelist_panel
            self.browser_html: str = ""
            self.uri: str = cfg['uri']
            self.fps: list[str] = self.cfg['fingerprints']
            self.nginx404: str = nginx_404
            self.resp = Response(self.nginx404, status=404, mimetype='text/html')
            self.BROWSER_UA = re.compile(r'(MSIE|Trident|(?!Gecko.+)Firefox|(?!AppleWebKit.+Chrome.+)Safari(?!.+Edge)|(?!AppleWebKit.+)Chrome(?!.+Edge)|(?!AppleWebKit.+Chrome.+Safari.+)Edge|AppleWebKit(?!.+Chrome|.+Safari)|Gecko(?!.+Firefox))(?: |\/)([\d\.apre]+)')
            self.RATIO: float = 1.073741824 # GiB to GB
            self.FILTERS: dict[str, str] = {
                'displayname': ':;"\'?/<>{}[]*&^%$#@\\|',
            }
            self.panels: list['XUiSession'] = list(panels)
            self.SALT: str = self.cfg['salt']
            with open('res/browser.html', 'r') as f:
                self.browser_html: str = f.read()
            if self.whitelist_panel:
                self.panels.append(self.whitelist_panel)
            self.start()

    def start(self):
        @self.app.route(f"/{self.uri}", strict_slashes=False)
        def _sub(): # type: ignore[reportUnusedFunction]
            return self.get_subscription(
                token=request.args.get('token', ''),
                lang=request.args.get('lang', ''),
                ua=request.headers.get('User-Agent', ''),
                ip=request.headers.get('X-Real-IP', '')
            )

    def hash(self, s: str) -> str:
        return hashlib.sha256((self.SALT + s).encode()).hexdigest()
     
    @staticmethod
    def compare(a: str, b: str) -> bool:
        return hmac.compare_digest(a, b)
    
    @staticmethod
    def isuuid(s: str) -> bool:
        """Validate a UUID."""
        try:
            val = uuid.UUID(s, version=4)
            return str(val) == s.lower()
        except ValueError:
            return False
    
    def isbrowser(self, ua: str) -> bool:
        """Parse a User-Agent against a regex to detect if it is a browser."""
        return bool(self.BROWSER_UA.search(ua))

    @staticmethod
    def sanitize(s: str) -> str:
        """Sanitize an external username"""
        s = s[:32]
        return re.sub(r'[^A-Za-z0-9_\-]', '', s)

    def usertotoken(self, token: str) -> str | None:
        """Get a username from a token, None if doesnt exist."""
        if len(token) < 30:
            return None
        reversed_tokens = {t: u for u, t in self.cfg["tokens"].items()}
        return reversed_tokens.get(token, None)

    def isuser(self, username: str) -> bool:
        """Know if a username exists."""
        if username and username in self.cfg['users']:
            return True
        return False

    @staticmethod
    def make_qr(text: str) -> io.BytesIO:
        img = qrcode.make(text)
        bio = io.BytesIO()
        img.save(bio, 'PNG')
        bio.seek(0)
        bio.name = "qr.png"
        return bio
    
    @staticmethod
    def restart(delay: int | float = 0.1) -> None:
        """Restart gunicorn with a delay (in seconds, defaults to 100ms).
        Redundant already, but i will keep it here."""
        def _restart():
            try:
                sig = signal.SIGHUP
            except NameError:
                raise RuntimeError("Must be ran on Linux.")
            time.sleep(delay)
            os.kill(os.getppid(), sig)
        threading.Thread(target=_restart, daemon=True).start()
    
    def getstatus(self, panel: XUiSession) -> ServerMetricsResponse | None:
        """Get the information about a panel."""
        try:
            x = panel.get(f"{panel.base_url}panel/api/server/status")
            data = x.json()
            if x.status_code not in [200]:
                self.log.error(f"getstatus fail: {data['msg']}")
                return None
            return data
        except Exception as e:
            self.log.error(f"getstatus fail: {e}")
            return None

    def getinbounds(self, panel: XUiSession) -> list[Inbound]:
        """Get inbounds list. Uses cache with TTL, panel.local (almost) skips cache."""
        now = time.time()
        ttl = 2 if panel.local else 15  # fast local, slow remote

        cached = panel.get_cache()
        if cached is not None and now - panel._cache_time < ttl: # type: ignore[reportPrivateUsage]
            return cached
        
        try:
            response = panel.get(f"{panel.base_url}panel/api/inbounds/list")
            data = response.json()
            if response.status_code not in [200] or not data.get("success"):
                self.log.error(f"getinbounds fail: {data.get('msg')}")
                return []
            inbounds = data['obj']
            if panel.ignore_inbounds:
                inbounds = [i for i in inbounds if i['id'] not in panel.ignore_inbounds]
            panel.set_cache(inbounds)
            return inbounds
        except Exception as e:
            self.log.warning(f"getinbounds fail: {e}")
            return []
    
    def _drop_cache(self, panel: XUiSession | None = None) -> None:
        """Drop cached inbounds. Call after mutations."""
        targets = [panel] if panel else list(self.panels)
        for p in targets:
            p.clear_cache()
    
    def _rollback_registered_user(
        self,
        *,
        username: str,
        ext_username: str,
        consumed_code: dict[str, Any] | None,
    ) -> None:
        """Best-effort rollback for register_with_code()."""
        with self.cfg as d:
            d.get("users", {}).pop(username, None)
            d.get("tokens", {}).pop(username, None)
            d.get("userFingerprints", {}).pop(username, None)
            d.get("status", {}).pop(username, None)
            d.get("statusTime", {}).pop(username, None)
            d.get("statusWl", {}).pop(username, None)
            d.get("displaynames", {}).pop(username, None)
            d.get("bw", {}).pop(username, None)
            d.get("wl_bw", {}).pop(username, None)
            d.get("time", {}).pop(username, None)
            d.get("webui_passwords", {}).pop(ext_username, None)
            d.get("webui_users", {}).pop(ext_username, None)
    
            if consumed_code is not None:
                codes = d.setdefault("codes", [])
                if not any(item.get("code") == consumed_code["code"] for item in codes):
                    codes.append(consumed_code)
    
    def bandwidth(self, 
                  username: str,
                  whitelist: bool = False
    ) -> BandwidthInfo:
        """Get bandwidth info about a user. Returns BandwidthInfo(upload, download, total). In bytes."""
        userid = self.cfg["users"][username]
        panels = [self.whitelist_panel] if whitelist else list(self.panels)
        if not whitelist and self.whitelist_panel: 
            panels.remove(self.whitelist_panel)
        
        if not panels:
            return BandwidthInfo(0, 0, 0)
        panels = cast(list[XUiSession], panels)
        up_total = 0
        down_total = 0
        for panel in panels:
            inbounds = self.getinbounds(panel)
            for i in inbounds:
                for v in (i['clientStats'] or []):
                    if v['uuid'] == userid:
                        up_total += v['up']
                        down_total += v['down']
        
        return BandwidthInfo(up_total, down_total, up_total + down_total)
    def get_bw_history(self, username: str, days: int = 30) -> list[BandwidthSnapshot]:
        """Return snapshots for a user, clamped to retention window."""
        cutoff = int(time.time()) - days * 86400
        user_data = self.bw_cfg.get("users", {}).get(username, {})
        raw = user_data.get("snapshots", [])
        return [BandwidthSnapshot(**s) for s in raw if s.get("ts", 0) >= cutoff]


    def add_users(self, username: str) -> None | str:
        """Add a user to panels. Dont confuse with add_new_user!
        str with error on error."""
        userid = self.cfg['users'][username]
        panels = self.panels
        
        for panel in panels:
            inbounds = self.getinbounds(panel)
            list_2_add: list[int] = []
            need_vision: list[int] = []
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
            
            payload: dict[str, list[SettingsClient]] = {"clients": [{
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
                data: dict[str, Any] = {
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
                    return response.json().get('msg')
                    
        self._drop_cache()
    def delete_user(self,
                    username: str,
                    perma: bool = False
    ) -> None | str:
        """Delete a user, either from panels or from storage too."""
        userid = self.cfg['users'][username]
        panels = self.panels

        for panel in panels:
            inbounds = self.getinbounds(panel)

            for j in inbounds:
                response = panel.post(
                    f"{panel.base_url}panel/api/inbounds/{str(j['id'])}/delClient/{userid}",
                    headers={'Accept': 'application/json'}
                )
                if not (response.status_code in [200, 201] and response.json().get('success')):
                    return response.json().get('msg', 'panel rejected update')


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

        self._drop_cache()
        return None
    def update_user(self, 
                    username: str, 
                    enable: bool | None = None, 
                    timee: bool | None = None, 
                    wl_enable: bool | None = None) -> None | str:
        """Disable/enable a user. wl_enable controls specifically the whitelist node.
        None on success."""
        userid = self.cfg['users'][username]
        if enable is not None:
            panels = list(self.panels)
            if self.whitelist_panel: panels.remove(self.whitelist_panel)
            for panel in panels:
                inbounds = self.getinbounds(panel)
                l = [i['id'] for i in inbounds if i['protocol'] == "vless"]
                the = {str(vi['id']): vx for vi in inbounds for vx in vi['clientStats'] if vx['uuid'] == userid}
                for k in l:
                    if str(k) not in the: continue 
                    payload = client_stats_to_settings(the[str(k)])
                    payload['enable'] = enable
                    payload['id'] = userid
                    response = panel.post(
                        f"{panel.base_url}panel/api/inbounds/updateClient/{userid}",
                        data={'id': k, 'settings': json.dumps({"clients": [payload]})},
                        headers={'Accept': 'application/json'}
                    )
                    
                    if not (response.status_code in [200, 201] and response.json().get('success')):
                        return response.json().get('msg', 'panel rejected update')
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
                payload = client_stats_to_settings(the[str(k)])
                payload['enable'] = wl_enable
                payload['id'] = userid
                
                panel.post(
                    f"{panel.base_url}panel/api/inbounds/updateClient/{userid}",
                    data={'id': k, 'settings': json.dumps({"clients": [payload]})},
                    headers={'Accept': 'application/json'}
                )
                
            with self.cfg as data:
                data.setdefault('statusWl', {})[username] = wl_enable
            
            self.log.info(f"Set WL status for {username} to {wl_enable}")
        self._drop_cache()
        return None
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
        timee: int = 0
    ) -> NewUserInfo | str:
        """Adds a new user. Returns string with error if any argument is incorrect.
        Now also suppports ext username and password (optional)"""
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
        
        try:
            x = self.add_users(username=username)
            if x is not None:
                self.log.error(f"add_new_user: {x}")
                return x
            return {"username": username, "token": token, "uuid": userid, "fingerprint": fingerprint, "displayname": displayname}
        finally:
            self._drop_cache()
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
        timee: int | None = None
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
            t['bw'][username] = [limit, t['bw'][username][1]]
            t['wl_bw'][username] = [wl_limit, t['wl_bw'][username][1]]
            t['time'][username] = timee
            if ext_password and ext_username:
                if _old_ext_username and _old_ext_username != ext_username:
                    t.get('webui_passwords', {}).pop(_old_ext_username, None)
                    t.get('webui_users', {}).pop(_old_ext_username, None)
                t['webui_passwords'][ext_username] = ext_password
                t['webui_users'][ext_username] = username                
        self._drop_cache()
    def update_uuid(self, username: str, uid: str) -> None | str:
        """Seperate method for updating the UUID. None on success, str on error.
        Potentially dangerous operation, seperate function."""
        if not self.isuser(username):
            return "Unknown username"
        olduid = self.cfg['users'][username]
        for panel in self.panels:
            inbounds = self.getinbounds(panel)
            l: list[int] = []
            need_vision: list[int] = []
            for i in inbounds:
                l.append(i['id'])
            the: dict[str, SettingsClient] = {}
            for vi in inbounds:
                for vx in vi['clientStats']:
                    if vx['uuid'] == olduid:
                        the[str(vi['id'])] = client_stats_to_settings(vx)
                        if json.loads(vi['streamSettings'])['network'] in ["tcp", "raw"]:
                            need_vision.append(vi['id'])
                        break
            for k in l:
                if str(k) not in the:
                    continue
                payload = the[str(k)]
                payload['id'] = uid
                payload['flow'] = "xtls-rprx-vision" if k in need_vision else ""
                data: dict[str, Any] = {
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
                    return response.json().get('msg')
        
        with self.cfg as t: t['users'][username] = uid
        self._drop_cache()

    def register_with_code(
        self,
        *,
        code: str,
        username: str,
        displayname: str,
        ext_username: str,
        ext_password: str,
    ) -> RegisterWithCodeInfo | str:
        """Create a new web user from a register code.
    
        Returns a user dict on success, or a human-readable error string on
        validation/business-rule failure.
    
        """
        if not code or not isinstance(code, str): # type: ignore[reportUnnecessaryIsInstance]
            return "Invalid code"
        if not username or not isinstance(username, str): # type: ignore[reportUnnecessaryIsInstance]
            return "Invalid username"
        if not isinstance(displayname, str): # type: ignore[reportUnnecessaryIsInstance]
            return "Invalid displayname"
        if not ext_username or not isinstance(ext_username, str): # type: ignore[reportUnnecessaryIsInstance]
            return "Invalid username"
        if not ext_password or not isinstance(ext_password, str): # type: ignore[reportUnnecessaryIsInstance]
            return "Invalid password"
    
        ext_username = self.sanitize(ext_username)
        if not ext_username:
            return "Invalid username"
        if len(ext_username) > 32:
            return "Ext Username too long"
    
        displayname = displayname.translate(
            str.maketrans("", "", self.FILTERS.get("displayname", ""))
        )
        if len(displayname) > 16:
            return "Displayname too long"
    
        token = ''.join(random.choices(string.ascii_letters + string.digits + '_-', k=40))
        userid = str(uuid.uuid4())
        fingerprint = random.choice(self.fps)
        hashed_password = self.hash(ext_password)
    
        consumed_code: dict[str, Any] | None = None
    
        with self.cfg as d:
            codes = d.setdefault("codes", [])
    
            match_index = None
            match_item = None
            for i, item in enumerate(codes):
                if item.get("code") == code:
                    match_index = i
                    match_item = item
                    break

            if match_item is None:
                return "Invalid code"
            if match_item.get("action") != "register":
                return "Invalid code"
    
            try:
                days = int(match_item.get("days", 0))
                gb = int(match_item.get("gb", 0))
                wl_gb = int(match_item.get("wl_gb", 0))
            except (TypeError, ValueError):
                return "Invalid code"
    
            users = d.setdefault("users", {})
            tokens = d.setdefault("tokens", {})
            user_fingerprints = d.setdefault("userFingerprints", {})
            status = d.setdefault("status", {})
            status_time = d.setdefault("statusTime", {})
            status_wl = d.setdefault("statusWl", {})
            displaynames = d.setdefault("displaynames", {})
            bw = d.setdefault("bw", {})
            wl_bw = d.setdefault("wl_bw", {})
            times = d.setdefault("time", {})
            webui_passwords = d.setdefault("webui_passwords", {})
            webui_users = d.setdefault("webui_users", {})

            if username in users:
                return "Username exists"
            if ext_username in webui_users:
                return "Ext Username exists"

            timee = int(time.time() + days * 86400) if days else 0
    
            if not match_item.get("perma", False):
                consumed_code = {
                    "code": code,
                    "action": "register",
                    "perma": False,
                    "days": days,
                    "gb": gb,
                    "wl_gb": wl_gb
                }
                del codes[match_index]
    
            users[username] = userid
            tokens[username] = token
            user_fingerprints[username] = fingerprint
            status[username] = True
            status_time[username] = True
            status_wl[username] = True
            displaynames[username] = displayname
            bw[username] = [gb, 0]
            wl_bw[username] = [wl_gb, 0]
            times[username] = timee
            webui_passwords[ext_username] = hashed_password
            webui_users[ext_username] = username

            result: RegisterWithCodeInfo = {
                "username": username,
                "token": token,
                "uuid": userid,
                "fingerprint": fingerprint,
                "limit": gb,
                "wl_limit": wl_gb,
                "time": timee,
            }

        try:
            self.add_users(username=username)
        except Exception as e:
            self.log.critical(f"register_with_code backend sync failed for {username}: {e}")
            self._rollback_registered_user(
                username=username,
                ext_username=ext_username,
                consumed_code=consumed_code,
            )
            raise RuntimeError(f"backend sync failed for {username}") from e
    
        return result

    def get_code(self, code: str) -> CodeObject | bool:
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
    def apply_bonus_code(self, *, username: str, code: str) -> ApplyBonusCodeObject | str:
        """Atomically validate/consume a bonus code and apply it to a user.
    
        Returns:
            dict: Applied bonus info on success.
            str: Human-readable error on validation/failure.
            """
        if not isinstance(username, str) or not username: # type: ignore[reportUnnecessaryIsInstance]
            return "Unknown user"
        if not isinstance(code, str) or not code: # type: ignore[reportUnnecessaryIsInstance]
            return "Unknown code"
    
        result: ApplyBonusCodeObject | None = None
    
        with self.cfg as d:
            users = d.setdefault("users", {})
            bw_map = d.setdefault("bw", {})
            wl_bw_map = d.setdefault("wl_bw", {})
            time_map = d.setdefault("time", {})
            codes = d.setdefault("codes", [])
    
            if username not in users:
                return "Unknown user"
            if username not in bw_map or username not in wl_bw_map or username not in time_map:
                return "Broken user state"
    
            code_index: int | None = None
            code_item: dict[str, Any] | None = None
    
            for i, item in enumerate(codes):
                if item.get("code") == code:
                    code_index = i
                    code_item = item
                    break
    
            if code_item is None or code_item.get("action") != "bonus":
                return "Unknown code"
    
            try:
                delta_days = int(code_item.get("days", 0))
                delta_gb = int(code_item.get("gb", 0))
                delta_wl_gb = int(code_item.get("wl_gb", 0))
            except (TypeError, ValueError):
                return "Invalid code"
    
            if delta_days < 0 or delta_gb < 0 or delta_wl_gb < 0:
                return "Invalid code"
    
            current_time = time_map[username]
            current_bw = bw_map[username]
            current_wl_bw = wl_bw_map[username]
    
            if not isinstance(current_time, int):
                return "Broken user state"
            if not isinstance(current_bw, list) or len(current_bw) < 2: # type: ignore
                return "Broken user state"
            if not isinstance(current_wl_bw, list) or len(current_wl_bw) < 2: # type: ignore
                return "Broken user state"
            if not isinstance(current_bw[0], int) or not isinstance(current_wl_bw[0], int):
                return "Broken user state"
    
            new_time = 0 if current_time == 0 else current_time + delta_days * 86400
            new_limit = 0 if current_bw[0] == 0 else current_bw[0] + delta_gb
            new_wl_limit = 0 if current_wl_bw[0] == 0 else current_wl_bw[0] + delta_wl_gb
    
            if not code_item.get("perma", False):
                del codes[code_index]  # type: ignore[index]
    
            current_bw[0] = new_limit
            current_wl_bw[0] = new_wl_limit
            time_map[username] = new_time
    
            result = {
                "days": delta_days,
                "gb": delta_gb,
                "wl_gb": delta_wl_gb,
                "perma": bool(code_item.get("perma", False)),
                "time": new_time,
                "limit": new_limit,
                "wl_limit": new_wl_limit,
            }
    
        return result or "Internal server error"

    def add_code(self, 
                 code: str, 
                 action: str, 
                 permanent: bool = False, 
                 days: int = 0, 
                 gb: int = 0,
                 wl_gb: int = 0) -> None | str:
        """Creates a code. Simple."""

        if not isinstance(code, str) or not code: # type: ignore[reportUnnecessaryIsInstance]
            return "code must be a non-empty string"
        if action not in ["register", "bonus"]:
            return f"action must be 'register' or 'bonus', got '{action}'"
        if not isinstance(permanent, bool): # type: ignore[reportUnnecessaryIsInstance]
            return "permanent must be a bool"
        if not all(isinstance(x, int) for x in (days, gb, wl_gb)): # type: ignore[reportUnnecessaryIsInstance]
            return "days, gb, wl_gb must be integers"
        if days < 0 or gb < 0 or wl_gb < 0:
            return "days, gb, wl_gb must be non-negative"
        
        with self.cfg as t:
            if any(c.get('code') == code for c in t['codes']):
                return f"code '{code}' already exists"
            t['codes'].append({
                "code": code, "action": action, "perma": permanent,
                "days": days, "gb": gb, "wl_gb": wl_gb
            })
        return None
    def delete_code(self, code: str) -> bool:
        """Returns True if deleted, False if not found."""
        def _delete(t: MutableMapping[str, Any]):
            for i, item in enumerate(cast(dict[CodeObject, str], t['codes'])):
                if item.get('code') == code:
                    del t['codes'][i]
                    return True
            return False
        return self.cfg.mutate(_delete)
    def list_code(self) -> list[str]:
        return [c['code'] for c in self.cfg['codes'] if 'code' in c]
    def bonus_code(self, value: int | str, code: str) -> ApplyBonusCodeObject | str:
        """Apply a bonus code for a Telegram user."""
        username = self.get_username_telegram(value)
        if not isinstance(username, str) or not username:
            return "Unknown Telegram user"
        return self.apply_bonus_code(username=username, code=code)

    def get_info(self, username: str, pretty: bool = False) -> UserInfo | None:
        """Get all info about a user. None if not found."""
        if not self.cfg['users'].get(username, None):
            return None
        
        conf = self.cfg.copy()
        bandwidths = self.bandwidth(username=username)
        wl_bandwidths = self.bandwidth(username=username, whitelist=True)
        monthly = conf['bw'][username][1]
        wl_monthly = conf['wl_bw'][username][1]
        domain = self.cfg['domain']
        if pretty:
            bandwidths = BandwidthInfo(*(round(x / 10**6, 2) for x in bandwidths))
            wl_bandwidths = BandwidthInfo(*(round(x / 10**6, 2) for x in wl_bandwidths))

        return {
            "_": random.choice(cast(list[str], conf.get('funny_strings', []))),
            "token": conf['tokens'][username],
            "link": f"{domain}/sub?token={conf['tokens'][username]}",
            "displayname": conf['displaynames'][username],
            "uuid": conf['users'][username],
            "fingerprint": conf['userFingerprints'][username],
            "enabled": conf['status'][username],
            "wl_enabled": conf['statusWl'][username],
            "time": conf['time'][username],
            "online": self.is_online(username),
            "bandwidth": {
                "total": {
                    "upload": bandwidths.upload,
                    "download": bandwidths.download,
                    "total": bandwidths.total
                },
                "wl_total": {
                    "upload": wl_bandwidths.upload,
                    "download": wl_bandwidths.download,
                    "total": wl_bandwidths.total
                },
                "monthly": monthly,
                "wl_monthly": wl_monthly,
                "limit": conf['bw'][username][0],
                "wl_limit": conf['wl_bw'][username][0]
            }
        }
    def get_info_telegram(self, tgid: int) -> UserInfo | None:
        """Returns all user info by telegram ID. None if target uid wasnt found."""
        username = cast(str, self.cfg['tgids'].get(str(tgid), None))
        if not username:
            return None
        return self.get_info(username, True)
    
    def is_registered(self, tgid: int) -> bool:
        """Check if a telegram user is already registered."""
        return str(tgid) in self.cfg['tgids']

    @overload
    def get_username_telegram(self, tgid: int | str, reverse: Literal[False] = False) -> str | None: ...
    
    @overload
    def get_username_telegram(self, tgid: int | str, reverse: Literal[True]) -> int | None: ...
    
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
        emails: dict[str, str] = {}
        for i in inbounds:
            for r in i['clientStats']:
                actual_email = r.get('email', '')
                parts = actual_email.rsplit('-', 1)
                parsed_username = parts[0] if len(parts) == 2 else actual_email
                if parsed_username == username:
                    emails[str(i['id'])] = actual_email
        return emails

    @overload
    def get_online_users(self, new: Literal[False] = False) -> list[str]: ...
    
    @overload
    def get_online_users(self, new: Literal[True]) -> dict[str, str | None]: ...
    
    def get_online_users(self, new: bool = False) -> list[str] | dict[str, str | None]:
        """Get the list of currently online users.
        new: False = ['username', ...]. True = {'username': ext_username_or_None, ...}"""
        online_users: set[str] = set()
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
    def reset_user(self, username: str) -> ResetUserObject | str:
        """Resets token and uuid to randomness. Dict with new values on success."""
        if not self.isuser(username):
            return "Unknown user"
        newid = str(uuid.uuid4())
        newt = ''.join(random.choices(string.ascii_lowercase + string.ascii_uppercase + string.digits, k=40))
        x = self.update_uuid(username, newid)
        if x is not None:
            self.log.critical(f"reset_user: error in update_uuid: {x}")
            return x
        xx = self.update_params(
            username=username,
            token=newt
        )
        if xx is not None:
            self.log.critical(f"reset_user: error in update_params: {xx}")
            return xx
    

        self._drop_cache()
        return {'uuid': newid, 'token': newt}

    @staticmethod
    def fmt_time(seconds: int) -> str:
        if seconds < 60:
            return f"{seconds}с"
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        d, h = divmod(h, 24)
        _ = s # Type checkers, just shut up already.
        if d > 0:
            return f"{d}д {h}ч {m}м"
        if h > 0:
            return f"{h}ч {m}м"
        return f"{m}м"
    
    def get_subscription(
        self, 
        *,
        token: str, 
        lang: str, 
        ua: str, 
        ip: str
    ) -> Response:
        """You alredy know what this is."""
        if not token:
            return self.resp
        username = self.usertotoken(token)
        self.log.info(f"""subscription hit!
IP: {ip}
User-Agent: {ua}
Username: {"none" if not username else username}
Lang: {lang}""")
        
        if not username:
            return self.resp
        if lang not in ["ru", "en"]:
            return self.resp
        bandwidths = self.bandwidth(username)

        cfg = self.cfg.copy()
        
        browser = self.isbrowser(ua=ua)
        name = cfg['displaynames'][username]
        need_dummy_link = "v2rayn" in ua.lower() or "v2rayng" in ua.lower()
        is_happ = ua.startswith("Happ/")
        mimetype = "text/plain" if not browser else "text/html"
        if browser:
            return Response(self.browser_html, mimetype=mimetype)

        status: bool = cfg['status'][username]
        statusTime: bool = cfg['statusTime'][username]
        statusWl: bool = cfg['statusWl'][username]
        times: int = cfg['time'][username]
        sub_name: str = cfg['sub_name']
        userinfo = "upload={upload};download={download};total={total};expire={expire}"

        desc = self.build_description(
            username=username,
            name=name,
            lang=lang,
            bandwidths=bandwidths,
            status=status,
            statusTime=statusTime,
            ts=times
        )
        announce = f"base64:{base64.b64encode(desc.encode('utf-8')).decode('utf-8')}"
        if status:
            upload = str(bandwidths[0]) if cfg['bw'][username][0] == 0 else str(int(cfg['bw'][username][1] / 2 * self.RATIO))
            download = str(bandwidths[1]) if cfg['bw'][username][0] == 0 else str(int(cfg['bw'][username][1] / 2 * self.RATIO))
            total = "0" if cfg['bw'][username][0] == 0 else str(int(cfg['bw'][username][0] * 10**9 * self.RATIO))
        else:
            upload = str(int(cfg['bw'][username][0] * 10**9 / 2 * self.RATIO))
            download = str(int(cfg['bw'][username][0] * 10**9 / 2 * self.RATIO))
            total = str(int(cfg['bw'][username][0] * 10**9 * self.RATIO))
        expire = str(times)

        userinfo = userinfo.format(
            upload=upload,
            download=download,
            total=total,
            expire=expire
        )
        
        headers: dict[str, Any] = {
            'Profile-Title': sub_name,
            'Subscription-Userinfo': userinfo,
            'profile-update-interval': "1",
            # funny
            'x-stop-looking-here': 'please',
            'x-pomidorka': '1515',
            'announce': announce,
            'Content-Type': "text/plain"
        }
        if is_happ: headers['routing'] = f"happ://routing/onadd/{base64.b64encode(json.dumps(cfg['routing']).encode('utf-8')).decode('utf-8')}"
        user_uuid: str = cfg['users'][username]
        generated_links: list[str] = []

        for p_key, p_name in cfg['profiles'].items():
            if p_key in cfg['whitelistProfiles'] and not statusWl:
                continue
            if not status:
                break
            link: str = cfg['masterLinks'][p_key]
            flag: str = cfg['flags'][p_key] if is_happ else ""
            node: str = cfg['profileNodes'][p_key]
            domain: str = cfg['nodes'][node]
            name: str = flag + p_name[0 if lang == "en" else 1]
            link = link.replace("DOMAIN", domain)
            link = link.replace("FINGERPRINT", cfg['userFingerprints'][username])
            link = link.replace("UUID", user_uuid)
            link = link.replace("NAME", name)
            if "EXTRA" in link:
                extra_data = cfg['xhttpExtra'].get(p_key)
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
            v, label = fmt_bytes_tuple(bandwidths.upload)
            dt = dt.replace("%s1", v).replace("%u1", label)
            v, label = fmt_bytes_tuple(bandwidths.download)
            dt = dt.replace("%s2", v).replace("%u2", label)

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
                          bandwidths: BandwidthInfo, 
                          status: bool,
                          statusTime: bool,
                          ts: int) -> str:
        cfg = self.cfg.copy()

        desc = cfg['description'][0] if lang == "en" else cfg['description'][1]
        if not status:
            desc = cfg['description'][2] if lang == "en" else cfg['description'][3]
        if status:
            v, label = fmt_bytes_tuple(int(bandwidths.upload))
            desc = desc.replace("%s1", v).replace("%u1", label)
            v, label = fmt_bytes_tuple(int(bandwidths.download))
            desc = desc.replace("%s2", v).replace("%u2", label)

            if cfg['bw'][username][0] != 0:
                desc = desc.replace("%x1", cfg['description'][4] if lang == "en" else cfg['description'][5])
            else:
                desc = desc.replace("%x1", "")
            if cfg['time'][username] != 0:
                desc = desc.replace("%t1", cfg['description'][6] if lang == "en" else cfg['description'][7])
                desc = desc.replace(
                    "%t3",
                    datetime.fromtimestamp(ts, tz=SERVER_TZ).strftime("%d.%m.%y %H:%M")
                )
                desc = desc.replace(
                    "%t2",
                    str((ts - int(time.time())) // 86400)
                )
            else:
                desc = desc.replace("%t1", "").replace("%t2", "").replace("%t3", "")
            if cfg['bw'][username][0] != 0:
                v, label = fmt_bytes_tuple(int(cfg['bw'][username][1]))
                desc = desc.replace("%n1", v).replace("%y1", label)
                desc = desc.replace("%n2", str(cfg['bw'][username][0])).replace("%y2", "GB")
            else:
                desc = desc.replace("%n1", "").replace("%y1", "").replace("%n2", "").replace("%y2", "")
            if cfg['wl_bw'][username][0] != 0:
                if cfg['wl_bw'][username][1] > cfg['wl_bw'][username][0] * 10**9: 
                    desc = desc.replace("%l1", cfg['description'][12] if lang == "en" else cfg['description'][13])
                else:
                    desc = desc.replace("%l1", cfg['description'][10] if lang == "en" else cfg['description'][11])
                
                v, label = fmt_bytes_tuple(int(cfg['wl_bw'][username][1]))
                desc = desc.replace("%w1", v).replace("%i1", label)

                desc = desc.replace("%w2", str(cfg['wl_bw'][username][0])).replace("%i2", "GB")
            else:
                desc = desc.replace("%l1", "").replace("%w1", "").replace("%i1", "").replace("%w2", "").replace("%i2", "")
        else:
            if cfg['bw'][username][0] != 0:
                v, label = fmt_bytes_tuple(int(cfg['bw'][username][1]))
                desc = desc.replace("%n1", v).replace("%u1", label)

                desc = desc.replace("%n2", str(cfg['bw'][username][0])).replace("%u2", "GB")
            if not statusTime:
                desc = desc.replace("%t1", cfg['description'][8] if lang == "en" else cfg['description'][9])
                desc = desc.replace(
                    "%t3",
                    datetime.fromtimestamp(ts, tz=SERVER_TZ).strftime("%d.%m.%y %H:%M")
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
                 bw_cfg: Config,
                 sub: Subscription, 
                 bot: PublicBotLike | None = None,
                 admin_bot: AdminBotLike | None = None):
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg: Config = cfg
            self.bw_cfg: Config = bw_cfg
            self._lock = threading.Lock()
            self._stop_event = threading.Event()
            self._snapshot_lock = threading.Lock()
            self.sub: Subscription = sub
            self.bot: PublicBotLike | None = bot
            self.admin_bot: AdminBotLike | None = admin_bot
            self.mem: dict[str, BandwidthInfo] = {}
            self.wl_mem: dict[str, BandwidthInfo] = {}
            self._snapshot_initialized: bool = False
            self._panel_alerts: dict[str, Any] = {} # only used by 1 thread, no lock needed yet
            self._panel_alert_cooldown: int = self.cfg.get('panel_alert_cooldown', None) or 3600

            self._threads: tuple[threading.Thread, ...] = (
                threading.Thread(target=self._every_120s, daemon=True, name="Quota & Notifs"),
                threading.Thread(target=self._every_2h, daemon=True, name="Date check"),
                threading.Thread(target=self._every_15s, daemon=True, name="Bandwidth"),
                threading.Thread(target=self._every_24h, daemon=True, name="Reset notifs & Prune BW Info"),
                threading.Thread(target=self._every_5m, daemon=True, name="Panels check"),
                threading.Thread(target=self._every_24h_snapshot, daemon=True, name="Daily BW Snapshot")
            )
    def start(self):
        initial_mem: dict[str, BandwidthInfo] = {}
        initial_wl_mem: dict[str, BandwidthInfo] = {}
        for i in list(self.cfg['users'].keys()):
            initial_wl_mem[i] = self.sub.bandwidth(username=i, whitelist=True)
            if self.cfg['bw'][i][0] == 0:
                continue
            initial_mem[i] = self.sub.bandwidth(username=i)

        with self._snapshot_lock:
            self.mem = initial_mem
            self.wl_mem = initial_wl_mem
            self._snapshot_initialized = True

        ### Start Threads ###

        for thread in self._threads:
            thread.start()
    
    def stop(self):
        self._stop_event.set()
        for thread in self._threads:
            thread.join(timeout=5)
        
    def _update_user(self, *args: Any, **kwargs: Any):
        x = self.sub.update_user(*args, **kwargs)
        if x is not None:
            self.log.critical(f"update_user error: {x}") 
    
    def prune_old_snapshots(self): 
        with self.bw_cfg as d:            
            retention = d.get("_meta", {}).get("retention_days", 30) # 30-day default
            cutoff = int(time.time()) - retention * 86400
            for user in d.get("users", {}):
                snapshots = d["users"][user]["snapshots"]
                d["users"][user]["snapshots"] = [s for s in snapshots if s["ts"] >= cutoff]                                                                                                                                                                                                     
            d["_meta"]["last_prune"] = int(time.time())

    def bandwidth_check(self):
        updates: dict[str, BandwidthUpdate] = {}    # username -> (delta, current) for main
        wl_updates: dict[str, BandwidthUpdate] = {} # username -> (delta, current) for whitelist
        for i in list(self.cfg['users'].keys()):
            # Main bandwidth
            if self.cfg['time'].get(i, 0) != 0:
                if (self.cfg['time'][i] - int(time.time())) >= 0 and not self.cfg['statusTime'][i]:
                    self._update_user(username=i, enable=True, timee=True)
            if self.cfg['bw'].get(i, [0, 0])[0] != 0:
                if self.cfg['bw'][i][1] < self.cfg['bw'][i][0] * 1000**3 and not self.cfg['status'][i]:
                    self._update_user(username=i, enable=True)
                try:
                    current_bws = self.sub.bandwidth(username=i)
                    with self._lock:
                        if i not in self.mem:
                            self.mem[i] = current_bws
                        else:
                            delta = int(current_bws.total - self.mem[i].total)
                            if delta > 0:
                                updates[i] = BandwidthUpdate(delta=delta, current=current_bws)
                                
                except Exception as e:
                    self.log.error(f"BW error for {i}: {e}")
            # Whitelist bandwidth
            if self.cfg['wl_bw'].get(i, [0, 0])[0] != 0:
                if self.cfg['wl_bw'][i][1] < self.cfg['wl_bw'][i][0] * 1000**3 and not self.cfg['statusWl'][i]:
                    self._update_user(username=i, wl_enable=True)
                try:
                    current_bws = self.sub.bandwidth(username=i, whitelist=True)
                    with self._lock:
                        if i not in self.wl_mem:
                            self.wl_mem[i] = current_bws
                        else:
                            delta = int(current_bws.total - self.wl_mem[i].total)
                            if delta > 0:
                                wl_updates[i] = BandwidthUpdate(delta=delta, current=current_bws)
                                
                except Exception as e:
                    self.log.critical(f"BW wl error for {i}: {e}")
        if not updates and not wl_updates:
            return

        with self.cfg as data:
            with self._lock:
                for i, update in updates.items():
                    if i in data['bw']:
                        data['bw'][i][1] += update.delta
                        self.mem[i] = update.current
                for i, update in wl_updates.items():
                    if i in data['wl_bw']:
                        data['wl_bw'][i][1] += update.delta
                        self.wl_mem[i] = update.current

    def panel_health_check(self):
        """Check each panel's Xray status and resource usage. Alert on issues."""
        for panel in self.sub.panels:
            try:
                status = self.sub.getstatus(panel)
                if not status:
                    continue
                
                key = panel.name
                problems: list[str] = []
                
                xray = status.get('xray', {})
                if xray.get('state') != 'running':
                    problems.append(f"Xray: {xray.get('state', 'unknown')} - {xray.get('errorMsg', '')}")
                
                cpu = status.get('cpu', 0)
                if cpu > 90:
                    problems.append(f"CPU: {cpu}%")
                
                mem = status.get('mem', {})
                if mem.get('total', 0) > 0:
                    mem_pct = (mem['current'] / mem['total']) * 100
                    if mem_pct > 90:
                        problems.append(f"RAM: {mem_pct:.0f}%")

                disk = status.get('disk', {})
                if disk.get('total', 0) > 0:
                    disk_pct = (disk['current'] / disk['total']) * 100
                    if disk_pct > 90:
                        problems.append(f"Disk: {disk_pct:.0f}%")
                
                if problems:
                    last = self._panel_alerts.get(key, 0)
                    if time.time() - last > self._panel_alert_cooldown:
                        self._panel_alerts[key] = time.time()
                        if self.admin_bot: 
                            msg = f"⚠️ Panel {key}:\n" + "\n".join(f"- {p}" for p in problems)
                            self.admin_bot.msg(msg)
                else:
                    self._panel_alerts.pop(key, None)
            except Exception as e:
                self.log.error(f"health check {panel.address}: {e}")

    def check(self):
        for i in list(self.cfg['users'].keys()):
            tg_user = self.sub.get_username_telegram(tgid=i, reverse=True)
            if self.cfg['time'][i] != 0:
                if (self.cfg['time'][i] - int(time.time())) <= 0:
                    if self.cfg['statusTime'][i]:
                        self._update_user(username=i, enable=False, timee=False)
                        if self.bot: self.bot.msg(tg_user, 'warning_disabled') # sub expired
                    continue
                else:
                    days = (self.cfg['time'][i] - int(time.time())) // 86400
                    if days <= 2:
                        if tg_user not in self.cfg['_notified']:
                            def _u_(t: MutableMapping[str, Any]): t['_notified'].append(tg_user)
                            self.cfg.mutate(_u_)
                            if self.bot: self.bot.msg(tg_user, 'warning_days', days=days)
            if self.cfg['wl_bw'][i][0] != 0 and self.cfg['wl_bw'][i][1] > self.cfg['wl_bw'][i][0] * 1000**3:
                if self.cfg['statusWl'].get(i, True):
                    self._update_user(username=i, wl_enable=False)
                    if self.bot: self.bot.msg(tg_user, 'warning_traffic_whitelist_disabled', available=self.cfg['wl_bw'][i][0])
            elif self.cfg['wl_bw'][i][0] != 0 and self.cfg['wl_bw'][i][1] > self.cfg['wl_bw'][i][0] * 1000**3 * 0.95: # 95%
                if tg_user not in self.cfg['_wl_notified']:
                    def _up(t: MutableMapping[str, Any]): t['_wl_notified'].append(tg_user)
                    self.cfg.mutate(_up)
                    if self.bot: self.bot.msg(tg_user, 'warning_traffic_whitelist', used=int(round(self.cfg['wl_bw'][i][1] / 10**6, 0)), available=self.cfg['wl_bw'][i][0])
            if not self.cfg['status'][i]:
                continue
            if self.cfg['bw'][i][0] == 0:
                continue
            if self.cfg['bw'][i][0] != 0 and self.cfg['bw'][i][1] > self.cfg['bw'][i][0] * 1000**3:
                self._update_user(username=i, enable=False, timee=True)
                if self.bot: self.bot.msg(tg_user, 'warning_traffic_disabled', available=self.cfg['bw'][i][0])
            elif self.cfg['bw'][i][0] != 0 and self.cfg['bw'][i][1] > self.cfg['bw'][i][0] * 1000**3 * 0.95: # 95%
                if tg_user not in self.cfg['_notified']:
                    def _up_(t: MutableMapping[str, Any]): t['_notified'].append(tg_user)
                    self.cfg.mutate(_up_)                    
                    if self.bot: self.bot.msg(tg_user, 'warning_traffic', used=int(round(self.cfg['bw'][i][1] / 10**6, 0)), available=self.cfg['bw'][i][0])

    def is_first(self):
        # NOTE: This function is NOT meant to be called like `bwatch_instance.is_first()`.
        # NOTE: Exclusive to one thread only.
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
        with self.cfg as t:
            t['_notified'] = []
            t['_wl_notified'] = []

    def record_daily_snapshot(self):
        """Record one bandwidth snapshot per user for today (UTC midnight).
        Reads mem/wl_mem under lock, writes to bw_history.json."""
        with self._snapshot_lock:
            midnight = int(time.time()) - (int(time.time()) % 86400)
            initialized = self._snapshot_initialized

            if not initialized:
                for username in list(self.cfg['users'].keys()):
                    current = self.sub.bandwidth(username=username)
                    wl_current = self.sub.bandwidth(username=username, whitelist=True)
                    self.mem[username] = current
                    self.wl_mem[username] = wl_current
                self._snapshot_initialized = True
                return

            with self.cfg as main:
                user_limits = {
                    u: (main['bw'][u][0], main['wl_bw'][u][0])
                    for u in main['users'].keys()
                }

            for username, (limit, wl_limit) in user_limits.items():
                if limit == 0 and wl_limit == 0:
                    continue

                last_mem = self.mem.get(username)
                last_wl_mem = self.wl_mem.get(username)

                current = self.sub.bandwidth(username=username)
                wl_current = self.sub.bandwidth(username=username, whitelist=True)

                self.mem[username] = current
                self.wl_mem[username] = wl_current

                if last_mem:
                    daily_up = int(current.upload - last_mem.upload)
                    daily_down = int(current.download - last_mem.download)
                else:
                    daily_up = 0
                    daily_down = 0

                if last_wl_mem:
                    wl_up = int(wl_current.upload - last_wl_mem.upload)
                    wl_down = int(wl_current.download - last_wl_mem.download)
                else:
                    wl_up = 0
                    wl_down = 0

                snap = BandwidthSnapshot(ts=midnight, up=daily_up, down=daily_down, wl_up=wl_up, wl_down=wl_down)

                with self.bw_cfg as d:
                    users = d.setdefault("users", {})
                    user_data = users.setdefault(username, {"snapshots": []})
                    snaps = user_data["snapshots"]

                    existing_idx = next((i for i, s in enumerate(snaps) if s["ts"] == midnight), None)
                    if existing_idx is not None:
                        snaps[existing_idx] = asdict(snap)
                    else:
                        snaps.insert(0, asdict(snap))
    
    ### Helper functions ###
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
            self.prune_old_snapshots()
    def _every_24h_snapshot(self):
        while not self._stop_event.wait(86400):
            self.record_daily_snapshot()
    def _every_5m(self):
        while not self._stop_event.wait(300):
            self.panel_health_check()
