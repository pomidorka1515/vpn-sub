from __future__ import annotations

from loggers import Logger
from session import XUiSession
from db import Database, CodeError, DuplicateError, UserRecord

import threading
import hashlib
import hmac
import re
import json
import string
import random
import uuid
import time
import copy
import base64
import signal
import urllib.parse
import os
import sys
import io
import qrcode

from flask import Flask, Response, request
from datetime import timedelta, datetime, timezone
from typing import Any, cast, NamedTuple, overload, Literal, Callable
from dacite import from_dict, Config as DConfig
from custom_types import (
    ServerMetricsResponse, Inbound, 
    SettingsClient, NewUserInfo,
    RegisterWithCodeInfo, CodeObject,
    UserInfo, UserInfoBandwidth, UserInfoBandwidthTotal, 
    ResetUserObject,
    ApplyBonusCodeObject, 
    BandwidthSnapshot, StateSnapshot,
    PublicBotLike, AdminBotLike, 
    client_stats_to_settings,
    ConfigLike, LinesConfigLike,
    JsonValue
)
from util import fmt_bytes_tuple, SysUtil
from dataclasses import asdict
from collections.abc import Mapping
from collections import deque

# pyright: reportUnnecessaryIsInstance=false

__all__ = [
    "Subscription", "BWatch", 
    "BandwidthInfo",
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
AUDIT_VALUES = Literal[
    'sub_hit',
    'user_refresh', 'user_delete', 'user_reset',
    'user_update', 'user_update_params', 'user_add', 
    'user_update_uuid', 'user_consume_code', 
    'code_add', 'code_delete',

]
SNAP_DACITE_CFG = DConfig(
    cast=[tuple]
)


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
    def format_bytes_mb(b: int) -> float:
        return round(b / 10**6, 2)
    
    def format_all_mb(self) -> BandwidthInfo:
        """Format everything into MB."""
        return BandwidthInfo(
            self.format_bytes_mb(int(self.upload)),
            self.format_bytes_mb(int(self.download)),
            self.format_bytes_mb(int(self.total))
        )

class BandwidthUpdate(NamedTuple):
    delta: int
    current: BandwidthInfo


class Subscription:
    def __init__(
        self, 
        cfg: ConfigLike,
        db: Database,
        app: Flask,
        panels: list[XUiSession],
        whitelist_panel: XUiSession | None,
        audit_cfg: LinesConfigLike | None = None
    ):
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg: ConfigLike = cfg
            self.db = db
            self.audit_cfg: LinesConfigLike | None = audit_cfg
            self.app: Flask = app
            self.whitelist_panel: XUiSession | None  = whitelist_panel
            self.uri: str = cfg['uri']
            self.fps: list[str] = self.cfg['fingerprints']
            self.nginx404: str = nginx_404
            self.resp = Response(self.nginx404, status=404, mimetype='text/html')
            self.BROWSER_UA = re.compile(r'(MSIE|Trident|(?!Gecko.+)Firefox|(?!AppleWebKit.+Chrome.+)Safari(?!.+Edge)|(?!AppleWebKit.+)Chrome(?!.+Edge)|(?!AppleWebKit.+Chrome.+Safari.+)Edge|AppleWebKit(?!.+Chrome|.+Safari)|Gecko(?!.+Firefox))(?: |\/)([\d\.apre]+)')
            self.RATIO: float = 1.073741824 # GiB to GB
            self.FILTERS: dict[str, str] = {
                'displayname': ':;"\'?/<>{}[]*&^%$#@\\|',
            }
            self.panels: list[XUiSession] = list(panels)
            self.SALT: str = self.cfg['salt']
            with open('res/browser.html', 'r') as f:
                self.browser_html: str = f.read()
            if self.whitelist_panel:
                self.panels.append(self.whitelist_panel)

            self.start()

    def start(self) -> None:
        @self.app.route(f"/{self.uri}", strict_slashes=False)
        def _sub() -> Response: # pyright: ignore[reportUnusedFunction]
            return self.get_subscription(
                token=request.args.get('token', ''),
                lang=request.args.get('lang', ''),
                ua=request.headers.get('User-Agent', ''),
                ip=request.headers.get('X-Real-IP', ''),
                force_json=request.args.get('force_json', '0')
            )

    def hash(self, s: str) -> str:
        return hashlib.sha256((self.SALT + s).encode()).hexdigest()

    def audit(
        self, 
        *,
        name: AUDIT_VALUES,
        info: Mapping[str, JsonValue] | None = None
    ) -> None:
        """Call a JSONL config manager to append an action. 
        Ignores everything if the audit config is not set."""
        if not self.audit_cfg:
            return
        ts = datetime.now(timezone.utc)
        cur_date = ts.strftime("%d.%m.%Y %H:%M:%S")
        to_log: Mapping[str, str | int | float | Mapping[str, JsonValue] | None] = {
            "ts": ts.timestamp(),
            "date": cur_date,
            "action": name,
            "info": info
        }
        self.audit_cfg.append(record=to_log)
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
        return self.db.token_to_user(token)

    def isuser(self, username: str) -> bool:
        """Know if a username exists."""
        return bool(username) and self.db.user_exists(username)

    def _user(self, username: str) -> UserRecord:
        user = self.db.get_user(username)
        if user is None:
            raise KeyError(username)
        return user

    def get_user_state(self, username: str) -> UserRecord:
        """Return a typed copy of the user's persisted state."""
        return self._user(username)

    def list_users(self) -> list[str]:
        """Return users in deterministic username order."""
        return self.db.list_users()

    def get_token(self, username: str) -> str:
        return str(self._user(username)["token"])

    def get_fingerprint(self, username: str) -> str:
        return str(self._user(username)["fingerprint"])

    def get_external_username(self, username: str) -> str | None:
        return self.db.user_to_ext(username)

    def external_username_exists(self, ext_username: str) -> bool:
        return self.db.ext_to_user(ext_username) is not None

    def validate_credentials(self, ext_username: str, password: str) -> str | None:
        stored = self.db.ext_password(ext_username)
        username = self.db.ext_to_user(ext_username)
        if stored is None or username is None or not self.compare(stored, self.hash(password)):
            return None
        return username

    def set_telegram_user(self, tgid: int | str, username: str | None) -> None:
        if username is None:
            existing = self.db.tgid_to_user(tgid)
            if existing is not None:
                self.db.set_telegram(existing, None)
            return
        self.db.set_telegram(username, str(tgid))

    def get_telegram_language(self, tgid: int | str) -> str:
        return self.db.get_telegram_language(tgid)

    def has_telegram_language(self, tgid: int | str) -> bool:
        return self.db.has_telegram_language(tgid)

    def set_telegram_language(self, tgid: int | str, language: str) -> None:
        self.db.set_telegram_language(tgid, language)
    
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
        def _restart() -> None:
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
            x = panel.get(f"panel/api/server/status")
            data: dict[str, object] = x.json()
            if x.status_code not in (200,):
                self.log.error(f"getstatus fail: {data['msg']}")
                return None
            return from_dict(ServerMetricsResponse, data)
        except Exception as e:
            self.log.error(f"getstatus fail: {e}")
            return None

    def getinbounds(self, panel: XUiSession) -> list[Inbound]:
        """Get inbounds list. Uses cache with TTL, panel.local (almost) skips cache."""
        now = time.time()
        ttl = 2 if panel.local else 15  # fast local, slow remote

        cached = panel.cache
        if cached is not None and now - panel.cache_time < ttl:
            return cached # NOTE: cache stores dataclasses!
        
        try:
            response = panel.get(f"panel/api/inbounds/list")
            data: dict[str, list[dict[str, object]]] = response.json()
            if response.status_code not in (200,) or not data.get("success"):
                self.log.error(f"getinbounds fail: {data.get('msg')}")
                return []
            raw_inbounds: list[dict[str, object]] = data['obj']
            inbounds = [from_dict(Inbound, i) for i in raw_inbounds]
            if panel.ignore_inbounds:
                inbounds = [i for i in inbounds if i.id not in panel.ignore_inbounds]
            panel.cache = inbounds
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
    ) -> None:
        """Best-effort rollback for register_with_code()."""
        # Registration is committed before the panel request so panel I/O is
        # never inside a SQLite transaction. The pending record makes removal
        # plus a finite-code refund one short local transaction.
        self.db.rollback_registration_sync(username)
    
    def bandwidth(self, 
                  username: str,
                  whitelist: bool = False
    ) -> BandwidthInfo:
        """Get bandwidth info about a user. Returns BandwidthInfo(upload, download, total). In bytes."""
        userid = str(self._user(username)["uuid"])

        if whitelist:
            # No whitelist panel configured -> there is nothing to report.
            # NOTE: don't fall through to the loop below with a bare `[None]` list,
            # `getinbounds()` dereferences attributes on `panel` immediately and
            # would raise an AttributeError on a None panel.
            if not self.whitelist_panel:
                return BandwidthInfo(0, 0, 0)
            panels: list[XUiSession] = [self.whitelist_panel]
        else:
            panels = list(self.panels)
            if self.whitelist_panel:
                panels.remove(self.whitelist_panel)

        if not panels:
            return BandwidthInfo(0, 0, 0)
        up_total = 0
        down_total = 0
        for panel in panels:
            inbounds = self.getinbounds(panel)
            for i in inbounds:
                for v in (i.clientStats or []):
                    if v.uuid == userid:
                        up_total += v.up
                        down_total += v.down
        
        return BandwidthInfo(up_total, down_total, up_total + down_total)
    def get_bw_history(self, username: str, days: int = 30) -> list[BandwidthSnapshot]:
        """Return snapshots for a user, clamped to retention window."""
        cutoff = int(time.time()) - days * 86400
        return [BandwidthSnapshot(**row) for row in self.db.get_bandwidth_snapshots(username, cutoff)]

    def get_snapshots(self, days: int = 30) -> list[StateSnapshot]:
        """Return state snapshots, clamped to a retention window."""
        cutoff = int(time.time()) - days * 86400
        return [from_dict(StateSnapshot, row, config=SNAP_DACITE_CFG)
                for row in self.db.get_state_snapshots(cutoff)]
        
    def leaderboard(
        self,
        category: Literal["total", "monthly", "wl_monthly"],
        *,
        top_n: int = 0,
        use_displaynames: bool = False,
        flip: bool = False
    ) -> dict[str, int]:
        """
        Dict of top-n users by specified bandwidth type, in order.
        WARNING: polls all users info. this is blocking!
        
        Args:
            category: type of bandwidth to use.
            top_n: clamp the leaderboard to top-n users.
                If <= 0, returns a leaderboard with all users.
            use_displaynames: if True, uses display names instead of internal usernames.
                Useful when passing data into chart.leaderboard_chart().
            flip: if True, sorted in ascending order, otherwise descending.

        Returns:
            A dict: {"username": 1293720471, "second_place": 12493630},
            where key is the username and value is bandwidth in bytes.
        """
        raw: dict[str, int] = {}
        users = self.list_users()
        if use_displaynames:
            display_users = [str(self._user(user)["displayname"]) for user in users]
        else:
            display_users = users
        # display_users is what we use in dict keys
        # populate the raw data
        match category:
            case 'total':
                for user, display in zip(users, display_users):
                    total = self.bandwidth(user).total
                    raw[display] = cast(int, total) or 0
            case 'monthly' | 'wl_monthly':
                for user, display in zip(users, display_users):
                    state = self._user(user)
                    if category == "monthly":
                        limit = state["bw_limit_gb"]
                        used = state["bw_used"]
                    else:
                        limit = state["wl_limit_gb"]
                        used = state["wl_used"]
                    if limit == 0:
                        continue # skip users who dont have bandwidth
                    raw[display] = used

        sorted_items: list[tuple[str, int]] = sorted(raw.items(), key=lambda x: x[1], reverse=not flip)
        if top_n > 0:
            sorted_items = sorted_items[:top_n]
        return {k: v for k, v in sorted_items}
    
    def add_users(self, username: str, _called_internally: bool = False) -> str | None:
        """Sync users to panels."""
        userid = str(self._user(username)["uuid"])
        panels = self.panels

        payload = SettingsClient(
            id=userid,
            flow="",
            email="",
            limitIp=0,
            totalGB=0,
            expiryTime=0,
            enable=True,
            tgId="",
            subId="",
            comment="",
            reset=0
        )

        for panel in panels:
            inbounds = self.getinbounds(panel)
            list_2_add: list[int] = []
            need_vision: list[int] = []
            for i in inbounds:
                if i.protocol != "vless":
                    self.log.debug(f"Non-VLESS inbound found ({i.protocol}). Ignoring.")
                    continue
                already_exists = any(v.uuid == userid for v in i.clientStats)
                if already_exists:
                    continue
                list_2_add.append(i.id)
                streamsettings = json.loads(i.streamSettings)
                if streamsettings['network'] in ('tcp', 'raw'):
                    need_vision.append(i.id)
            
            
            for j in list_2_add:
                client = payload
                client.email = f"{username}-{''.join(random.choices(string.ascii_lowercase + string.digits, k=8))}"
                client.flow = "xtls-rprx-vision" if j in need_vision else ""
                data: dict[str, int | str] = {
                    'id': j,
                    'settings': json.dumps({"clients": [asdict(client)]})
                }

                resp = panel.post(
                    url=f"panel/api/inbounds/addClient",
                    data=data,
                    headers={'Accept': 'application/json'}
                )
                content = resp.json()
                if not (resp.status_code in (200, 201) and content.get('success')):
                    err_msg: str = content.get('msg', 'unknown error')
                    return err_msg
        if not _called_internally: self.audit(name="user_refresh", info={"username":username})
        self._drop_cache()
        return None
    def delete_user(self,
                    username: str,
                    perma: bool = False
    ) -> None | str:
        """Delete a user, either from panels or from storage too."""
        userid = str(self._user(username)["uuid"])
        panels = self.panels

        for panel in panels:
            inbounds = self.getinbounds(panel)

            for j in inbounds:
                response = panel.post(
                    f"panel/api/inbounds/{str(j.id)}/delClient/{userid}",
                    headers={'Accept': 'application/json'}
                )
                content = response.json()
                if not (response.status_code in (200, 201) and content.get('success')):
                    err_msg: str = content.get('msg', 'panel rejected update')
                    return err_msg
        if perma:
            self.db.delete_user(username)

        self.audit(name="user_delete", info={"username": username, "perma": perma})
        self._drop_cache()
        return None
    def update_user(self, 
                    username: str, 
                    enable: bool | None = None, 
                    timee: bool | None = None, 
                    wl_enable: bool | None = None) -> None | str:
        """Disable/enable a user. wl_enable controls specifically the whitelist node.
        None on success."""
        userid = str(self._user(username)["uuid"])
        audit_info: dict[str, str | bool] = {"username": username}
        if enable is not None:
            panels = list(self.panels)
            if self.whitelist_panel: panels.remove(self.whitelist_panel)
            for panel in panels:
                inbounds = self.getinbounds(panel)
                l = [i.id for i in inbounds if i.protocol == "vless"]
                the = {str(vi.id): vx for vi in inbounds for vx in vi.clientStats if vx.uuid == userid}
                for k in l:
                    if str(k) not in the: continue 
                    payload = client_stats_to_settings(the[str(k)])
                    payload.enable = enable
                    payload.id = userid
                    response = panel.post(
                        f"panel/api/inbounds/updateClient/{userid}",
                        data={'id': k, 'settings': json.dumps({"clients": [asdict(payload)]})},
                        headers={'Accept': 'application/json'}
                    )
                    content = response.json()
                    if not (response.status_code in (200, 201) and content.get('success')):
                        err_msg: str = content.get('msg', 'panel rejected update')
                        return err_msg
            
            fields: dict[str, object] = {"status": enable}
            if timee is not None:
                fields["status_time"] = timee
            self.db.update_user(username, **fields)
            
            audit_info['enable'] = enable
        if wl_enable is not None:
            # Only push the change to the whitelist panel if one is actually
            # configured. Regardless of that, the local `statusWl` bookkeeping
            # below must still be updated -- otherwise (see the `enable` branch
            # above, which always writes to cfg) callers silently get a "success"
            # (None) return value while the requested state never gets recorded,
            # and BWatch.check() would keep re-triggering the same disable/enable
            # every cycle since `statusWl` never flips.
            if self.whitelist_panel:
                panel = self.whitelist_panel
                inbounds = self.getinbounds(panel)
                l = [i.id for i in inbounds if i.protocol == "vless"]
                the = {str(vi.id): vx for vi in inbounds for vx in vi.clientStats if vx.uuid == userid}

                for k in l:
                    if str(k) not in the: continue
                    payload = client_stats_to_settings(the[str(k)])
                    payload.enable = wl_enable
                    payload.id = userid

                    response = panel.post(
                        f"panel/api/inbounds/updateClient/{userid}",
                        data={'id': k, 'settings': json.dumps({"clients": [asdict(payload)]})},
                        headers={'Accept': 'application/json'}
                    )
                    content = response.json()
                    if not (response.status_code in (200, 201) and content.get('success')):
                        _err_msg: str = content.get('msg', 'panel rejected update')
                        return _err_msg

            self.db.update_user(username, status_wl=wl_enable)

            audit_info['wl_enable'] = wl_enable

        self.audit(name="user_update", info=audit_info)
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
        else:
            ext_username = None


        displayname = displayname.translate(str.maketrans('', '', self.FILTERS['displayname']))
        if len(displayname) > 16:
            return "Displayname too long"
        try:
            self.db.create_user(
                username=username, uuid=userid, token=token, fingerprint=fingerprint,
                displayname=displayname, expires_at=timee, bw_limit_gb=limit,
                wl_limit_gb=wl_limit, ext_username=ext_username,
                ext_password_hash=ext_password,
            )
        except DuplicateError:
            return "Username or external username exists"
        
        try:
            resp = self.add_users(username=username, _called_internally=True)
            if isinstance(resp, str):
                self.db.delete_user(username)
                return f"Panel error: {resp}"
            return NewUserInfo(
                username=username,
                token=token,
                uuid=userid,
                fingerprint=fingerprint,
                displayname=displayname
            )
        except Exception:
            self.db.delete_user(username)
            raise
        finally:
            to_log: dict[str, str] = {"username": username}
            if ext_username:
                to_log['ext_username'] = ext_username
            self.audit(name="user_add", info=to_log)
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
        current = self._user(username)
        audit_info: dict[str, str | int] = {"username": username}
        if displayname is None:
            displayname = str(current["displayname"])
        else:
            displayname = displayname.translate(str.maketrans('', '', self.FILTERS['displayname']))
            if len(displayname) > 16:
                return "Displayname too long"
            audit_info['displayname'] = displayname
        token = token if token is not None else str(current["token"])
        if fingerprint is None:
            fingerprint = str(current["fingerprint"])
        else:
            audit_info['fingerprint'] = fingerprint
        if fingerprint not in self.fps:
            return "Invalid fingerprint"
        limit = int(current["bw_limit_gb"]) if limit is None else limit
        wl_limit = int(current["wl_limit_gb"]) if wl_limit is None else wl_limit
        timestamp = int(current["expires_at"]) if timee is None else timee
        if timestamp > 2**31:
            return "Invalid time"
        old_ext = current["ext_username"]
        if ext_username is None:
            ext_username = str(old_ext) if old_ext is not None else None
        else:
            ext_username = self.sanitize(ext_username)
            if len(ext_username) > 32:
                return "Username too long"
            audit_info['ext_username'] = ext_username
        if ext_password is None:
            password_hash = current["ext_password_hash"]
        else:
            password_hash = self.hash(ext_password)
        try:
            self.db.update_user(
                username, displayname=displayname, token=token, fingerprint=fingerprint,
                bw_limit=limit, wl_bw_limit=wl_limit, expiry_time=timestamp,
                ext_username=ext_username, ext_password=password_hash,
            )
        except DuplicateError:
            return "Ext username exists"

        self.audit(name="user_update_params", info=audit_info)
        self._drop_cache()
        return None

    def _rollback_user_uuid(
        self,
        username: str,
        old_uid: str,
        new_uid: str,
        successful: list[tuple[XUiSession, int, SettingsClient, bool]],
    ) -> None:
        """Restore old UUID on panels that were already updated before a failure.
        Logs but never raises — rollback failures are logged, not propagated."""
        for panel, inbound_id, settings, had_vision in successful:
            settings.id = old_uid
            settings.flow = "xtls-rprx-vision" if had_vision else ""
            data: dict[str, int | str] = {
                'id': inbound_id,
                'settings': json.dumps({"clients": [asdict(settings)]})
            }
            try:
                resp = panel.post(
                    f"panel/api/inbounds/updateClient/{new_uid}",
                    data=data,
                    headers={'Accept': 'application/json'}
                )
                if not (resp.ok and resp.json().get('success')):
                    self.log.error(
                        f"_rollback_user_uuid: panel {panel.name} inbound {inbound_id} "
                        f"failed to restore UUID {old_uid}: {resp.json().get('msg', 'unknown')}"
                    )
            except Exception as e:
                self.log.error(
                    f"_rollback_user_uuid: panel {panel.name} inbound {inbound_id} "
                    f"exception during rollback: {e}"
                )

    def update_uuid(self, username: str, uid: str) -> None | str:
        """Seperate method for updating the UUID. None on success, str on error.
        Potentially dangerous operation, seperate function."""
        if not self.isuser(username):
            return "Unknown username"
        if not self.isuuid(uid):
            return "Invalid UUID"
        olduid = str(self._user(username)["uuid"])
        successful: list[tuple['XUiSession', int, SettingsClient, bool]] = []

        for panel in self.panels:
            inbounds = self.getinbounds(panel)
            l: list[int] = []
            need_vision: list[int] = []
            for i in inbounds:
                l.append(i.id)
            the: dict[str, SettingsClient] = {}
            for vi in inbounds:
                for vx in vi.clientStats:
                    if vx.uuid == olduid:
                        the[str(vi.id)] = client_stats_to_settings(vx)
                        if json.loads(vi.streamSettings)['network'] in ("tcp", "raw"):
                            need_vision.append(vi.id)
                        break
            for k in l:
                if str(k) not in the:
                    continue
                payload = the[str(k)]
                payload.id = uid
                payload.flow = "xtls-rprx-vision" if k in need_vision else ""
                data: dict[str, int | str] = {
                    'id': k,
                    'settings': json.dumps({"clients": [asdict(payload)]})
                }
                response = panel.post(
                    f"panel/api/inbounds/updateClient/{olduid}",
                    data=data,
                    headers={'Accept': 'application/json'}
                )
                if not (response.status_code in (200, 201) and response.json().get('success')):
                    err_msg: str = response.json().get('msg', 'panel rejected update')
                    self.log.critical(f"update_uuid failed on panel {panel.name}: {err_msg}")
                    self._rollback_user_uuid(username, olduid, uid, successful)
                    return err_msg

                successful.append((panel, k, the[str(k)], k in need_vision))

        try:
            self.db.update_user(username, uuid=uid)
        except DuplicateError:
            self._rollback_user_uuid(username, olduid, uid, successful)
            return "UUID exists"
        self.audit(name="user_update_uuid", info={"username": username, "uuid": uid})
        self._drop_cache()
        return None

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
        if not code or not isinstance(code, str):
            return "Invalid code"
        if not username or not isinstance(username, str):
            return "Invalid username"
        if not ext_username or not isinstance(ext_username, str): 
            return "Invalid username"
        if not ext_password or not isinstance(ext_password, str):
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
    
        try:
            result_data = self.db.register_with_code(
                code=code, username=username, uuid=userid, token=token,
                fingerprint=fingerprint, displayname=displayname,
                ext_username=ext_username, ext_password_hash=hashed_password,
            )
        except (CodeError, DuplicateError) as exc:
            return "Username exists" if isinstance(exc, DuplicateError) else "Invalid code"
        result = RegisterWithCodeInfo(
            username=username, token=token, uuid=userid, fingerprint=fingerprint,
            limit=int(result_data["gb"]), wl_limit=int(result_data["wl_gb"]),
            time=int(result_data["time"]),
        )
        audit_result: Mapping[str, str | int] = {
            "username": username, "ext_username": ext_username, "uuid": userid,
            "fingerprint": fingerprint, "limit": int(result_data["gb"]),
            "wl_limit": int(result_data["wl_gb"]), "time": int(result_data["time"]),
        }
        try:
            panel_error = self.add_users(username=username, _called_internally=True)
            if panel_error is not None:
                self._rollback_registered_user(username=username)
                return f"Panel error: {panel_error}"
        except Exception as e:
            self.log.critical(f"register_with_code backend sync failed for {username}: {e}")
            self._rollback_registered_user(username=username)
            raise RuntimeError(f"backend sync failed for {username}") from e
        self.db.confirm_registration_sync(username)
        self.audit(name="user_add", info=audit_result)
        return result

    def get_code(self, code: str) -> CodeObject | bool:
        """Search for a code. Returns a dict if found, False if isnt."""

        result = self.db.get_code(code)

        if result:
            return CodeObject(
                code=code,
                action=result['action'],
                perma=result['perma'],
                uses=result['uses'],
                days=result['days'],
                gb=result['gb'],
                wl_gb=result['wl_gb']
            )
        else:
            return False
        
    def apply_bonus_code(self, *, username: str, code: str) -> ApplyBonusCodeObject | str:
        """Atomically validate/consume a bonus code and apply it to a user.
    
        Returns:
            dict: Applied bonus info on success.
            str: Human-readable error on validation/failure.
            """
        if not isinstance(code, str) or not code:
            return "Unknown code"
        try:
            result_data = self.db.adjust_bonus(username, code)
        except CodeError as exc:
            return str(exc).capitalize()
        result = ApplyBonusCodeObject(
            days=int(result_data["days"]), gb=int(result_data["gb"]),
            wl_gb=int(result_data["wl_gb"]), uses=int(result_data["uses"]),
            perma=bool(result_data["perma"]), time=int(result_data["time"]),
            limit=int(result_data["limit"]), wl_limit=int(result_data["wl_limit"]),
        )
        self.audit(name="user_consume_code", info=asdict(result))
        return result

    def add_code(self, 
        code: str, 
        action: str, 
        permanent: bool = False, 
        days: int = 0, 
        gb: int = 0,
        wl_gb: int = 0,
        uses: int = 1
    ) -> None | str:
        """
        Creates a code.
        If permanent is True, 'uses' param is ignored.
        """
        
        if not isinstance(code, str) or not code: 
            return "code must be a non-empty string"
        if action not in ("register", "bonus"):
            return f"action must be 'register' or 'bonus', got '{action}'"
        if not all(isinstance(x, int) for x in (days, gb, wl_gb, uses)): 
            return "days, gb, wl_gb must be integers"
        if days < 0 or gb < 0 or wl_gb < 0:
            return "days, gb, wl_gb must be non-negative"
        if not permanent:
            if uses < 1:
                return "uses must be >= 1"
        
        res: dict[str, str | bool | int] = {
            "code": code, "action": action, "perma": permanent,
            "days": days, "gb": gb, "wl_gb": wl_gb, "uses": uses if not permanent else -1
        }
        try:
            self.db.add_code(code, action, permanent=permanent, days=days, gb=gb, wl_gb=wl_gb, uses=uses)
        except DuplicateError:
            return f"code '{code}' already exists"
        self.audit(name="code_add", info=res)
        
        return None
    def delete_code(self, code: str) -> bool:
        """Returns True if deleted, False if not found."""
        deleted = self.db.delete_code(code)
        if deleted:
            self.audit(name="code_delete", info={"code": code})
        return deleted
    def list_code(self) -> list[str]:
        return [str(c['code']) for c in self.db.all_codes() if 'code' in c]
    def bonus_code(self, value: int | str, code: str) -> ApplyBonusCodeObject | str:
        """Apply a bonus code for a Telegram user."""
        username = self.get_username_telegram(value)
        if not isinstance(username, str) or not username:
            return "Unknown Telegram user"
        return self.apply_bonus_code(username=username, code=code)

    def get_info(self, username: str, pretty: bool = False) -> UserInfo | None:
        """Get all info about a user. None if not found."""
        if not self.isuser(username):
            return None
        
        conf = self.cfg.copy()
        user = self._user(username)
        bandwidths = self.bandwidth(username=username)
        wl_bandwidths = self.bandwidth(username=username, whitelist=True)
        monthly = int(user['bw_used'])
        wl_monthly = int(user['wl_used'])
        domain = self.cfg['domain']
        if pretty:
            bandwidths = bandwidths.format_all_mb()
            wl_bandwidths = wl_bandwidths.format_all_mb()
        return UserInfo(
            _=random.choice(cast(list[str], conf.get('funny_strings', []))),
            token=str(user['token']),
            link=f"{domain}/sub?token={user['token']}",
            displayname=str(user['displayname']),
            uuid=str(user['uuid']),
            fingerprint=str(user['fingerprint']),
            enabled=bool(user['enabled']),
            wl_enabled=bool(user['enabled_wl']),
            time=int(user['expires_at']),
            online=self.is_online(username),
            bandwidth=UserInfoBandwidth(
                total=UserInfoBandwidthTotal(
                    upload=bandwidths.upload,
                    download=bandwidths.download,
                    total=bandwidths.total
                ),
                wl_total=UserInfoBandwidthTotal(
                    upload=wl_bandwidths.upload,
                    download=wl_bandwidths.download,
                    total=wl_bandwidths.total
                ),
                monthly=monthly,
                wl_monthly=wl_monthly,
                limit=int(user['bw_limit_gb']),
                wl_limit=int(user['wl_limit_gb'])
            )

        )
    def get_info_telegram(self, tgid: int) -> UserInfo | None:
        """Returns all user info by telegram ID. None if target uid wasnt found."""
        username = self.db.tgid_to_user(tgid)
        if not username:
            return None
        return self.get_info(username, True)
    
    def is_registered(self, tgid: int) -> bool:
        """Check if a telegram user is already registered."""
        return self.db.tgid_to_user(tgid) is not None

    @overload
    def get_username_telegram(self, tgid: int | str, reverse: Literal[False] = False) -> str | None: ...
    
    @overload
    def get_username_telegram(self, tgid: int | str, reverse: Literal[True]) -> int | None: ...
    
    def get_username_telegram(self, tgid: int | str, reverse: bool = False) -> int | str | None:
        """Get the internal username for a tgid.
        Parameter reverse: if True, get tg id from username. Otherwise default behaviour."""

        if not reverse:
            return self.db.tgid_to_user(tgid)
        mapped = self.db.user_to_tgid(str(tgid))
        return int(mapped) if mapped is not None else None

    def get_emails(self, username: str, panel: XUiSession) -> dict[str, str]:
        """Get the panel emails. {'inboundId': 'actual_panel_email', ...}"""
        inbounds = self.getinbounds(panel=panel)
        emails: dict[str, str] = {}
        for i in inbounds:
            for r in i.clientStats:
                actual_email = r.email
                parts = actual_email.rsplit('-', 1)
                parsed_username = parts[0] if len(parts) == 2 else actual_email
                if parsed_username == username:
                    emails[str(i.id)] = actual_email
        return emails

    @overload
    def get_online_users(self, new: Literal[False] = False) -> list[str]: ...
    
    @overload
    def get_online_users(self, new: Literal[True]) -> dict[str, str | None]: ...
    
    @overload
    def get_online_users(self, new: bool) -> list[str] | dict[str, str | None]: ...

    def get_online_users(self, new: bool = False) -> list[str] | dict[str, str | None]:
        """Get the list of currently online users.
        new: False = ['username', ...]. True = {'username': ext_username_or_None, ...}"""
        online_users: set[str] = set()
        for panel in self.panels:
            if panel.dead:
                continue
            try:
                res = panel.post(f"panel/api/inbounds/onlines")
                if res.status_code in (200, 201) and res.json().get('success'):
                    for email in res.json().get('obj', []):
                        name_candidate = email.rsplit('-', 1)[0]
                        if self.db.user_exists(name_candidate):
                            online_users.add(name_candidate)
            except Exception as e:
                self.log.error(f"Online check error: {e}")
        
        if not new:
            return list(online_users)
        return {name: self.db.user_to_ext(name) for name in online_users}
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

        self.audit(name="user_reset", info={"username": username, "uuid": newid, "token": "redacted"})
        self._drop_cache()
        return ResetUserObject(uuid=newid, token=newt)



    def get_subscription(
        self, 
        *,
        token: str, 
        lang: str, 
        ua: str, 
        ip: str,
        force_json: str
    ) -> Response:
        """You alredy know what this is."""
        if not token:
            return self.resp
        username = self.usertotoken(token)
        self.audit(name='sub_hit', info={"username": username, "lang": lang, "ua": ua, "ip": ip, "force_json": force_json})
        
        if not username:
            return self.resp
        if lang not in ("ru", "en"):
            return self.resp
        bandwidths = self.bandwidth(username)

        cfg = self.cfg.copy()
        user = self._user(username)
        
        browser = self.isbrowser(ua=ua)
        displayname = str(user['displayname'])
        need_dummy_link = "v2rayn" in ua.lower() or "v2rayng" in ua.lower()
        is_happ = ua.startswith("Happ/")
        mimetype = "text/plain" if not browser else "text/html"
        uri: str = cfg['uri']
        if browser:
            return Response(self.browser_html, mimetype=mimetype)

        status = bool(user['enabled'])
        statusTime = bool(user['enabled_time'])
        statusWl = bool(user['enabled_wl'])
        times = int(user['expires_at'])
        sub_name: str = cfg['sub_name']
        userinfo = "upload={upload};download={download};total={total};expire={expire}"

        desc = self._build_description(
            cfg=cfg,
            username=username,
            name=displayname,
            lang=lang,
            bandwidths=bandwidths,
            status=status,
            statusTime=statusTime,
            ts=times,
            bw_limit=int(user['bw_limit_gb']),
            bw_used=int(user['bw_used']),
            wl_limit=int(user['wl_limit_gb']),
            wl_used=int(user['wl_used']),
        )
        announce = f"base64:{base64.b64encode(desc.encode('utf-8')).decode('utf-8')}"
        if status:
            upload = str(bandwidths[0]) if int(user['bw_limit_gb']) == 0 else str(int(int(user['bw_used']) / 2 * self.RATIO))
            download = str(bandwidths[1]) if int(user['bw_limit_gb']) == 0 else str(int(int(user['bw_used']) / 2 * self.RATIO))
            total = "0" if int(user['bw_limit_gb']) == 0 else str(int(int(user['bw_limit_gb']) * 10**9 * self.RATIO))
        else:
            upload = str(int(int(user['bw_limit_gb']) * 10**9 / 2 * self.RATIO))
            download = str(int(int(user['bw_limit_gb']) * 10**9 / 2 * self.RATIO))
            total = str(int(int(user['bw_limit_gb']) * 10**9 * self.RATIO))
        expire = str(times)

        userinfo = userinfo.format(
            upload=upload,
            download=download,
            total=total,
            expire=expire
        )
        
        headers: dict[str, str] = {
            'Profile-Title': sub_name,
            'Subscription-Userinfo': userinfo,
            'profile-update-interval': "1",
            # funny
            'x-stop-looking-here': 'please',
            'x-pomidorka': '1515',
            'announce': announce,
            'Content-Type': "text/plain"
        }

        provider_id = cfg['provider_id']

        if provider_id and is_happ:
            fallback_domain: str | None = cfg.get('fallback_domain', None)
            provider_id_headers: dict[str, str] = {
                'providerid': provider_id,
                'per-app-proxy-mode': 'bypass',
                'per-app-proxy-list': ','.join(cfg['bypass_packages']),
                'no-limit-xhttp-enabled': '1',
                'check-url-via-proxy': cfg.get('ping_check_url', 'https://google.com/generate_204'),
                'ping-type': 'proxy',
                'sniffing-enable': '1', # Routing works better
                'ping-result': 'time',
                'dont-use-filter': '1',
                'manual-block-user-agent': '1',
                'subscriptions-sort-type': 'without',
                'proxy-ping-timeout': '5' # NOTE: iOS only for some reason
            }
            if fallback_domain is not None:
                provider_id_headers['fallback-url'] = \
                    f'{fallback_domain.strip('/')}/{uri.strip('/')}?token={token}&lang={lang}{"&force_json=" + force_json if force_json else ""}'
                
            headers.update(provider_id_headers)

        user_uuid = str(user['uuid'])

        ### Content Generation ###

        mode: Literal['b64', 'json'] = 'b64'

        if is_happ or force_json == '1':
            mode = 'json'
        else:
            mode = 'b64'

        if mode == 'b64':
            # Backwards compat, TODO: remove this entirely
            if is_happ: headers['routing'] = f"happ://routing/onadd/{base64.b64encode(json.dumps(cfg['routing']).encode('utf-8')).decode('utf-8')}"
            payload = self._build_link_array(
                cfg=cfg,
                status=status,
                statusWl=statusWl,
                lang=lang,
                username=username,
                bandwidths=bandwidths,
                user_uuid=user_uuid,
                is_happ=is_happ,
                need_dummy_link=need_dummy_link,
                fingerprint=str(user['fingerprint'])
            )
            return Response(payload, mimetype=mimetype, headers=headers)
        
        elif mode == 'json':
            headers['Content-Type'] = 'application/json'
            json_payload = self._build_json(
                cfg=cfg,
                user_uuid=user_uuid,
                username=username,
                lang=lang,
                fingerprint=str(user['fingerprint'])
            )
            return Response(
                response=json.dumps(json_payload, ensure_ascii=False),
                mimetype='application/json; charset=utf-8',
                headers=headers
            )

    @staticmethod
    def _build_description(
        cfg: dict[str, Any],
        username: str,
        name: str,
        lang: str,
        bandwidths: BandwidthInfo, 
        status: bool,
        statusTime: bool,
        ts: int,
        bw_limit: int,
        bw_used: int,
        wl_limit: int,
        wl_used: int,
    ) -> str:
        descTable: list[str] = cfg['description']
        desc = descTable[0] if lang == "en" else descTable[1]
        if not status:
            desc = descTable[2] if lang == "en" else descTable[3]
        if status:
            v, label = fmt_bytes_tuple(int(bandwidths.upload))
            desc = desc.replace("%s1", v).replace("%u1", label)
            v, label = fmt_bytes_tuple(int(bandwidths.download))
            desc = desc.replace("%s2", v).replace("%u2", label)

            if bw_limit != 0:
                desc = desc.replace("%x1", descTable[4] if lang == "en" else descTable[5])
            else:
                desc = desc.replace("%x1", "")
            if ts != 0:
                desc = desc.replace("%t1", descTable[6] if lang == "en" else descTable[7])
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
            if bw_limit != 0:
                v, label = fmt_bytes_tuple(bw_used)
                desc = desc.replace("%n1", v).replace("%y1", label)
                desc = desc.replace("%n2", str(bw_limit)).replace("%y2", "GB")
            else:
                desc = desc.replace("%n1", "").replace("%y1", "").replace("%n2", "").replace("%y2", "")
            if wl_limit != 0:
                if wl_used > wl_limit * 10**9:
                    desc = desc.replace("%l1", descTable[12] if lang == "en" else descTable[13])
                else:
                    desc = desc.replace("%l1", descTable[10] if lang == "en" else descTable[11])
                
                v, label = fmt_bytes_tuple(wl_used)
                desc = desc.replace("%w1", v).replace("%i1", label)

                desc = desc.replace("%w2", str(wl_limit)).replace("%i2", "GB")
            else:
                desc = desc.replace("%l1", "").replace("%w1", "").replace("%i1", "").replace("%w2", "").replace("%i2", "")
        else:
            if bw_limit != 0:
                v, label = fmt_bytes_tuple(bw_used)
                desc = desc.replace("%n1", v).replace("%u1", label)

                desc = desc.replace("%n2", str(bw_limit)).replace("%u2", "GB")
            if not statusTime:
                desc = desc.replace("%t1", descTable[8] if lang == "en" else descTable[9])
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

    @staticmethod
    def _build_link_array(
        cfg: dict[str, Any],
        status: bool,
        statusWl: bool,
        lang: str,
        username: str,
        is_happ: bool,
        user_uuid: str,
        bandwidths: BandwidthInfo,
        need_dummy_link: bool,
        fingerprint: str,
    ) -> str:
        """Build a base64-encoded link array."""
        generated_links: deque[str] = deque()

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
            link = link.replace("FINGERPRINT", fingerprint)
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
            generated_links.appendleft(dummy)
        raw_text = "\n".join(generated_links)
        payload = base64.b64encode(raw_text.encode('utf-8')).decode('utf-8')

        return payload
    
    @staticmethod
    def _build_json(
        cfg: dict[str, Any],
        user_uuid: str,
        username: str,
        lang: str,
        fingerprint: str,
    ) -> list[dict[str, object]]:
        """Build an array of profiles for Happ."""
        obj: list[dict[str, object]] = []
        template: dict[str, object] = cfg['json_template']
        index: Literal[0, 1] = 0 if lang == "en" else 1 # Language index

        for p_key, p_name_list in cfg['profiles'].items():
            flag: str = cfg['flags'][p_key]
            p_name_raw: str = p_name_list[index]
            p_name: str = flag + p_name_raw
            node: str = cfg['profileNodes'][p_key]
            domain: str = cfg['nodes'][node]
            short_desc: str = cfg['shortProfileDescriptions'][p_key][index]
            is_reality: bool = False
            result: dict[str, Any] = copy.deepcopy(template)
            result['remarks'] = p_name
            result['outbounds'][0] = cfg['json_profiles'][p_key]
            result['outbounds'][0]['settings']['vnext'][0]['users'][0]['id'] = user_uuid
            result['outbounds'][0]['settings']['vnext'][0]['address'] = domain
            if result['outbounds'][0]['streamSettings'].get('tlsSettings', None) is not None:
                result['outbounds'][0]['streamSettings']['tlsSettings']['serverName'] = domain
                result['outbounds'][0]['streamSettings']['tlsSettings']['fingerprint'] = fingerprint
            if result['outbounds'][0]['streamSettings'].get('realitySettings', None) is not None:
                is_reality = True
                result['outbounds'][0]['streamSettings']['realitySettings']['fingerprint'] = fingerprint
            if result['outbounds'][0]['streamSettings'].get('xhttpSettings', None) is not None:
                if not is_reality:
                    result['outbounds'][0]['streamSettings']['xhttpSettings']['host'] = domain
            if result['outbounds'][0]['streamSettings'].get('grpcSettings', None) is not None:
                if not is_reality:
                    result['outbounds'][0]['streamSettings']['grpcSettings']['authority'] = domain
            if result['outbounds'][0]['streamSettings'].get('wsSettings', None) is not None:
                wsSettings: dict[str, Any] = result['outbounds'][0]['streamSettings']['wsSettings']
                wsSettings.setdefault('headers', {})
                wsSettings['host'] = domain
                wsSettings['headers']['Host'] = domain
            if result['outbounds'][0]['streamSettings'].get('httpupgradeSettings', None) is not None:
                httpupgradeSettings: dict[str, object] = result['outbounds'][0]['streamSettings']['httpupgradeSettings']
                httpupgradeSettings['host'] = domain
            meta = result.setdefault('meta', {})
            meta['serverDescription'] = short_desc # NOTE: this only works if you have a providerid,
                                                   # NOTE: but we set it regardless
            obj.append(result)
        
        return obj


class BWatch:
    """Class for monitoring bandwidth.
    Dependencies: Subscription
    Classes depending on this: Api, WebApi"""
    def __init__(
        self, 
        cfg: ConfigLike, 
        db: Database,
        sub: Subscription, 
        bot: PublicBotLike | None = None,
        admin_bot: AdminBotLike | None = None
    ):
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg: ConfigLike = cfg
            self.db = db
            self._stop_event = threading.Event()
            self._mem_lock = threading.Lock()  # single lock for all mem/wl_mem access
            self.sub: Subscription = sub
            self.bot: PublicBotLike | None = bot
            self.admin_bot: AdminBotLike | None = admin_bot
            self.mem: dict[str, BandwidthInfo] = {}
            self.wl_mem: dict[str, BandwidthInfo] = {}
            self._snapshot_initialized: bool = False
            self._panel_alerts: dict[str, int | float] = {} # only used by 1 thread, no lock needed yet
            self._panel_alert_cooldown: int = self.cfg.get('panel_alert_cooldown', as_type=int) or 3600

            _threads: tuple[tuple[Callable[..., object], str], ...] = (
                (self._every_120s, "Quota & Notifs"),
                (self._every_2h, "Date check"),
                (self._every_15s, "Bandwidth"),
                (self._every_24h, "Reset notifs & Prune old snapshots"),
                (self._every_5m, "Panels check"),
                (self._every_24h_snapshot, "Daily snapshots"),
            )
            self._threads: tuple[threading.Thread, ...] = tuple(
                threading.Thread(target=target, name=name, daemon=True)
                for target, name in _threads
            )

    def start(self) -> None:
        initial_mem: dict[str, BandwidthInfo] = {}
        initial_wl_mem: dict[str, BandwidthInfo] = {}
        for i in self.sub.list_users():
            initial_wl_mem[i] = self.sub.bandwidth(username=i, whitelist=True)
            if self.sub.get_user_state(i)['bw_limit_gb'] == 0:
                continue
            initial_mem[i] = self.sub.bandwidth(username=i)

        with self._mem_lock:
            self.mem = initial_mem
            self.wl_mem = initial_wl_mem
            self._snapshot_initialized = True

        # Run first snapshot immediately (record_daily_snapshot does its own locking)
        self.record_daily_snapshot()
        self.record_snap_snapshot()
        self.is_first()   # also run the monthly reset check immediately

        ### Start Threads ###

        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        for thread in self._threads:
            thread.join(timeout=5)
        
    def _update_user(self, *args: Any, **kwargs: Any) -> None:
        x = self.sub.update_user(*args, **kwargs)
        if x is not None:
            self.log.critical(f"update_user error: {x}") 
    
    # prune_old_<cfg name>_snapshots

    def prune_old_bw_snapshots(self) -> None:
        retention = int(self.db.get_metadata("bw_retention_days", "30") or 30)
        cutoff = int(time.time()) - retention * 86400
        self.db.prune_bandwidth_snapshots(cutoff)
    
    def prune_old_snap_snapshots(self) -> None:
        retention = int(self.db.get_metadata("state_retention_days", "30") or 30)
        cutoff = int(time.time()) - retention * 86400
        self.db.prune_state_snapshots(cutoff)
    
    def bandwidth_check(self) -> None:
        updates: dict[str, BandwidthUpdate] = {}    # username -> (delta, current) for main
        wl_updates: dict[str, BandwidthUpdate] = {} # username -> (delta, current) for whitelist
        for i in self.sub.list_users():
            state = self.sub.get_user_state(i)
            # Main bandwidth
            if int(state['expires_at']) != 0:
                if (int(state['expires_at']) - int(time.time())) >= 0 and not bool(state['enabled_time']):
                    self._update_user(username=i, enable=True, timee=True)
            if int(state['bw_limit_gb']) != 0:
                if int(state['bw_used']) < int(int(state['bw_limit_gb']) * 10**9) and not bool(state['enabled']):
                    self._update_user(username=i, enable=True)
                try:
                    current_bws = self.sub.bandwidth(username=i)
                    with self._mem_lock:
                        if i not in self.mem:
                            self.mem[i] = current_bws
                        else:
                            delta = int(current_bws.total - self.mem[i].total)
                            if delta > 0:
                                updates[i] = BandwidthUpdate(delta=delta, current=current_bws)

                except Exception as e:
                    self.log.error(f"BW error for {i}: {e}")
            # Whitelist bandwidth
            if int(state['wl_limit_gb']) != 0:
                if int(state['wl_used']) < int(int(state['wl_limit_gb']) * 10**9) and not bool(state['enabled_wl']):
                    self._update_user(username=i, wl_enable=True)
                try:
                    current_bws = self.sub.bandwidth(username=i, whitelist=True)
                    with self._mem_lock:
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

        with self._mem_lock:
            for i, update in updates.items():
                self.db.increment_usage(i, regular=update.delta)
                self.mem[i] = update.current
            for i, update in wl_updates.items():
                self.db.increment_usage(i, whitelist=update.delta)
                self.wl_mem[i] = update.current

    def panel_health_check(self) -> None:
        """Check each panel's Xray status and resource usage. Alert on issues."""
        for panel in self.sub.panels:
            try:
                status = self.sub.getstatus(panel)
                if not status:
                    continue
                
                key = panel.name
                problems: list[str] = []
                obj = status.obj
                obj.format()

                xray = obj.xray
                if xray.state != 'running':
                    problems.append(f"Xray: {xray.state} - {xray.errorMsg}")
                
                cpu = obj.cpu
                if cpu > 90:
                    problems.append(f"CPU: {cpu}%")
                
                mem = obj.mem
                if mem.total > 0:
                    mem_pct = (mem.current / mem.total) * 100
                    if mem_pct > 90:
                        problems.append(f"RAM: {mem_pct:.0f}%")

                disk = obj.disk
                if disk.total > 0:
                    disk_pct = (disk.current / disk.total) * 100
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

    def check(self) -> None:
        for i in self.sub.list_users():
            state = self.sub.get_user_state(i)
            tg_user = self.sub.get_username_telegram(tgid=i, reverse=True)
            expires_at = int(state['expires_at'])
            bw_limit = int(state['bw_limit_gb'])
            bw_used = int(state['bw_used'])
            wl_limit = int(state['wl_limit_gb'])
            wl_used = int(state['wl_used'])
            if expires_at != 0:
                if (expires_at - int(time.time())) <= 0:
                    if bool(state['enabled_time']):
                        self._update_user(username=i, enable=False, timee=False)
                        if self.bot: self.bot.msg(tg_user, 'warning_disabled') # sub expired
                    continue
                else:
                    days = (expires_at - int(time.time())) // 86400
                    if days <= 2 and tg_user is not None and self.db.mark_notification("regular", tg_user):
                            if self.bot: self.bot.msg(tg_user, 'warning_days', days=days)
            if wl_limit != 0 and wl_used > int(wl_limit * 10**9):
                if bool(state['enabled_wl']):
                    self._update_user(username=i, wl_enable=False)
                    if self.bot: self.bot.msg(tg_user, 'warning_traffic_whitelist_disabled', available=wl_limit)
            elif wl_limit != 0 and wl_used > int(wl_limit * 10**9 * 0.95) and tg_user is not None and self.db.mark_notification("whitelist", tg_user):
                    if self.bot: self.bot.msg(tg_user, 'warning_traffic_whitelist', used=int(round(wl_used / 10**6, 0)), available=wl_limit)
            if not bool(state['enabled']):
                continue
            if bw_limit == 0:
                continue
            if bw_used > int(bw_limit * 10**9):
                self._update_user(username=i, enable=False, timee=True)
                if self.bot: self.bot.msg(tg_user, 'warning_traffic_disabled', available=bw_limit)
            elif bw_used > int(bw_limit * 10**9 * 0.95) and tg_user is not None and self.db.mark_notification("regular", tg_user):
                    if self.bot: self.bot.msg(tg_user, 'warning_traffic', used=int(round(bw_used / 10**6, 0)), available=bw_limit)

    def is_first(self) -> None:
        # NOTE: This function is NOT meant to be called like `bwatch_instance.is_first()`.
        # NOTE: Exclusive to one thread only.
        now = datetime.now()
        if now.day != 1:
            return
        today = now.strftime("%Y-%m-%d")

        # Restart-safe: compare against stored month key, not just the date string.
        # '_last_reset_month' stores "YYYY-MM" so a process restart on day 2
        # doesn't accidentally re-trigger a reset that already happened.
        current_month = now.strftime("%Y-%m")
        self.db.reset_monthly(current_month, today)
    
    def reset(self) -> None:
        self.db.clear_notifications()

    def record_snap_snapshot(self) -> None:
        """Record one state snapshot (`SysUtil` + panels) for today."""
        panels_data: dict[str, object] = {}
        for panel in self.sub.panels:
            status = self.sub.getstatus(panel)
            if status is not None:
                panels_data[panel.name] = asdict(status.obj)

        midnight = int(time.time()) - (int(time.time()) % 86400)
        data: dict[str, object] = {
            "ts": midnight,
            "host": asdict(SysUtil.full_info()),
            "panels": panels_data
        }

        self.db.upsert_state_snapshot(midnight, data)
        
        self.prune_old_snap_snapshots()

    def record_daily_snapshot(self) -> None:
        """Record one bandwidth snapshot per user for today (UTC midnight).

        Thread-safe: takes a copy of mem under lock, releases it for I/O,
        then updates mem + writes config under lock."""

        # 1. Snapshot mem under lock
        with self._mem_lock:
            mem_snapshot = dict(self.mem)
            wl_mem_snapshot = dict(self.wl_mem)

        # 2. Fetch bandwidth outside lock (I/O can be slow)
        midnight = int(time.time()) - (int(time.time()) % 86400)
        # {username: (current, wl_current, delta_up, delta_down, wl_up, wl_down)}
        snapshot_data: dict[str, tuple[BandwidthInfo, BandwidthInfo, int, int, int, int]] = {}

        for username in self.sub.list_users():
            state = self.sub.get_user_state(username)
            bw_limit = int(state['bw_limit_gb'])
            wl_limit = int(state['wl_limit_gb'])
            if bw_limit == 0 and wl_limit == 0:
                continue

            try:
                current = self.sub.bandwidth(username=username)
                wl_current = self.sub.bandwidth(username=username, whitelist=True)
            except Exception:
                continue
            
            last_mem = mem_snapshot.get(username)
            last_wl_mem = wl_mem_snapshot.get(username)

            snapshot_data[username] = (
                current, wl_current,
                int(current.upload - last_mem.upload) if last_mem else 0,
                int(current.download - last_mem.download) if last_mem else 0,
                int(wl_current.upload - last_wl_mem.upload) if last_wl_mem else 0,
                int(wl_current.download - last_wl_mem.download) if last_wl_mem else 0,
            )

        # 3. Update mem + write snapshot under lock (atomic)
        with self._mem_lock:
            for username, (current, wl_current, up, down, wl_up, wl_down) in snapshot_data.items():
                self.mem[username] = current
                self.wl_mem[username] = wl_current

                self.db.upsert_bandwidth_snapshot(username, midnight, up, down, wl_up, wl_down)

        self.prune_old_bw_snapshots()
    
    ### Helper functions ###
    def _every_120s(self) -> None:
        while not self._stop_event.wait(120):
            self.check()
    def _every_2h(self) -> None:
        while not self._stop_event.wait(7200):
            self.is_first()
    def _every_15s(self) -> None:
        while not self._stop_event.wait(15):
            self.bandwidth_check()
    def _every_24h(self) -> None:
        while not self._stop_event.wait(86400):
            self.reset()
            self.prune_old_bw_snapshots()
            self.prune_old_snap_snapshots()
    def _every_24h_snapshot(self) -> None:
        while not self._stop_event.wait(86400):
            try:
                self.record_daily_snapshot()
            except Exception:
                self.log.exception("Daily bw snapshot failed: ")
            try:
                self.record_snap_snapshot()
            except Exception:
                self.log.exception("Daily state snapshot failed: ")
    def _every_5m(self) -> None:
        while not self._stop_event.wait(300):
            self.panel_health_check()
