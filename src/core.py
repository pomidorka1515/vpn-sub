from __future__ import annotations

from errors import (
    AppError,
    ConflictError,
    NotFoundError,
    PanelUnavailableError,
    PanelRejectedError,
    ValidationError,
    DuplicateError,
    CodeError,
    UnsupportedPlatformError
)
from loggers import Logger
from session import XUiSession
from db import Database

import hashlib
import json
import string
import random
import uuid
import time
import io
import qrcode
import platform
import secrets
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from flask import Flask, Response, request
from datetime import datetime, timezone
from typing import cast, NamedTuple, overload, Literal
from dacite import from_dict, Config as DConfig
from custom_types import (
    ServerMetricsResponse, Inbound, 
    SettingsClient, NewUserInfo,
    RegisterWithCodeInfo, CodeObject,
    UserInfo, UserInfoBandwidth, UserInfoBandwidthTotal, 
    ResetUserObject,
    ApplyBonusCodeObject, 
    BandwidthSnapshot, StateSnapshot,
    BandwidthInfo,
    UserRecord,
    client_stats_to_settings,
)
from config import (
    ConfigLike, LinesConfigLike,
    JsonValue
)
from builders import *
from util import isuuid, compare, sanitize
from dataclasses import asdict
from collections.abc import Mapping

# pyright: reportUnnecessaryIsInstance=false

__all__ = [
    "Subscription",
    "OnlineStatus"
]

if platform.system().lower() != 'linux': # avoid pylance static evaluation
    raise UnsupportedPlatformError()

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


class OnlineStatus(NamedTuple):
    users: list[str] | dict[str, str | None]
    panel_health: dict[str, Literal["ok", "unavailable", "invalid"]]


class Subscription:
    def __init__(
        self, 
        cfg: ConfigLike,
        lang_cfg: ConfigLike, # ro
        db: Database,
        app: Flask,
        panels: list[XUiSession],
        whitelist_panel: XUiSession | None,
        audit_cfg: LinesConfigLike | None = None
    ):
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg: ConfigLike = cfg
            self.lang_cfg: ConfigLike = lang_cfg
            self.db: Database = db
            self.audit_cfg: LinesConfigLike | None = audit_cfg
            self.app: Flask = app
            self.whitelist_panel: XUiSession | None  = whitelist_panel
            self.uri: str = cfg['uri']
            self.fps: list[str] = self.cfg['fingerprints']

            self.panels: list[XUiSession] = list(panels)
            self.SALT: str = self.cfg['salt']
            self.password_hasher = PasswordHasher()
            with open('res/browser.html', 'r') as f:
                self.browser_html: str = f.read()
            if self.whitelist_panel:
                self.panels.append(self.whitelist_panel)

            self.register_routes()

    def register_routes(self) -> None:
        @self.app.route(f"/{self.uri}", strict_slashes=False)
        def _sub() -> tuple[Response, int]: # pyright: ignore[reportUnusedFunction]
            return get_subscription(
                self,
                token=request.args.get('token', ''),
                lang=request.args.get('lang', ''),
                ua=request.headers.get('User-Agent', ''),
                ip=request.headers.get('X-Real-IP', ''),
                force_json=request.args.get('force_json', '0')
            )

    def hash(self, s: str) -> str:
        return self.password_hasher.hash(s)

    def legacy_hash(self, s: str) -> str:
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
            raise NotFoundError("Unknown username")
        return user

    def get_user_state(self, username: str) -> UserRecord:
        """Return a typed copy of the user's persisted state."""
        return self._user(username)

    def list_users(self) -> list[str]:
        """Return users in deterministic username order."""
        return self.db.list_users()

    def get_token(self, username: str) -> str:
        return str(self._user(username)["token"])

    def auth_token_to_user(self, auth_token: str) -> str | None:
        """Get a username from a web auth token, None if it does not exist."""
        if len(auth_token) != 100:
            return None
        return self.db.auth_token_to_user(auth_token)

    def set_auth_token(self, username: str, auth_token: str | None) -> None:
        self._user(username)
        self.db.set_auth_token(username, auth_token)

    def get_fingerprint(self, username: str) -> str:
        return str(self._user(username)["fingerprint"])

    def get_external_username(self, username: str) -> str | None:
        return self.db.user_to_ext(username)

    def external_username_exists(self, ext_username: str) -> bool:
        return self.db.ext_to_user(ext_username) is not None

    def validate_credentials(self, ext_username: str, password: str) -> str | None:
        stored = self.db.ext_password(ext_username)
        username = self.db.ext_to_user(ext_username)
        if stored is None or username is None:
            return None
        if stored.startswith("$argon2id$"):
            try:
                self.password_hasher.verify(stored, password)
            except VerifyMismatchError:
                return None
            except (InvalidHashError, VerificationError):
                self.log.error(
                    "Corrupt or tampered argon2 password hash for %s",
                    ext_username,
                    exc_info=True,
                )
                return None
        else:
            if compare(stored, self.legacy_hash(password)):
                self.db.set_user(username, ext_password=self.hash(password))
                return username
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
    
    def getstatus(self, panel: XUiSession) -> ServerMetricsResponse | None:
        """Get panel status, or ``None`` when the panel status is unknown."""
        try:
            response = panel.get("panel/api/server/status")
            data: dict[str, object] = response.json()
            if response.status_code != 200:
                message = data.get("msg") or response.status_code
                raise PanelUnavailableError(f"Panel {panel.name} status query failed: {message}")
            return from_dict(ServerMetricsResponse, data)
        except Exception:
            self.log.error("panel status is unknown for %s", panel.name, exc_info=True)
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
                raise PanelUnavailableError(
                    f"Panel {panel.name} inbound query failed: "
                    f"{data.get('msg') or response.status_code}"
                )
            raw_inbounds: list[dict[str, object]] = data['obj']
            inbounds = [from_dict(Inbound, i) for i in raw_inbounds]
            if panel.ignore_inbounds:
                inbounds = [i for i in inbounds if i.id not in panel.ignore_inbounds]
            panel.cache = inbounds
            return inbounds
        except AppError:
            raise
        except Exception as exc:
            raise PanelUnavailableError(
                f"Panel {panel.name} inbound query failed: {exc}"
            ) from exc
    
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
        try:
            self.db.rollback_registration_sync(username)
        except Exception:
            self.log.critical(
                "register rollback failed for user %s; pending registration may remain",
                username,
                exc_info=True,
            )
            try:
                self.db.set_metadata(f"registration_rollback_failed:{username}", str(int(time.time())))
            except Exception:
                self.log.critical(
                    "failed to persist registration rollback failure marker for user %s",
                    username,
                    exc_info=True,
                )

    def recover_rollback_failures(self) -> None:
        """Retry persisted registration rollbacks at startup."""
        prefix = "registration_rollback_failed:"
        for key in tuple(self.db.list_metadata(prefix)):
            username = key.removeprefix(prefix)
            try:
                self.db.rollback_registration_sync(username)
                self.db.delete_metadata(key)
                self.log.warning("recovered registration rollback marker for user %s", username)
            except Exception:
                self.log.critical(
                    "registration rollback recovery failed for user %s; marker retained",
                    username,
                    exc_info=True,
                )

    def get_rollback_failures(self) -> dict[str, dict[str, dict[str, str]]]:
        """Return persisted rollback markers for admin reconciliation."""
        result: dict[str, dict[str, dict[str, str]]] = {"uuid": {}, "registration": {}}
        for key, value in self.db.list_metadata("uuid_rollback_failed:").items():
            timestamp, separator, reason = value.partition(":")
            result["uuid"][key.removeprefix("uuid_rollback_failed:")] = {
                "ts": timestamp,
                "reason": reason if separator else "",
            }
        for key, value in self.db.list_metadata("registration_rollback_failed:").items():
            result["registration"][key.removeprefix("registration_rollback_failed:")] = {
                "ts": value,
                "reason": "",
            }
        return result

    def clear_rollback_failure(self, kind: Literal["uuid", "registration"], username: str) -> None:
        """Clear a rollback marker after an administrator repairs the user."""
        if kind not in ("uuid", "registration"):
            raise ValidationError("Invalid rollback marker kind")
        self.db.delete_metadata(f"{kind}_rollback_failed:{username}")

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
            panels = [p for p in self.panels if p != self.whitelist_panel]

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
                        break
        
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
    
    def add_users(self, username: str, _called_internally: bool = False) -> None:
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
                    raise PanelRejectedError("Panel rejected user update")
        if not _called_internally: self.audit(name="user_refresh", info={"username":username})
        self._drop_cache()
    def delete_user(self,
                    username: str,
                    perma: bool = False
    ) -> None:
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
                    raise PanelRejectedError("Panel rejected user deletion")
        if perma:
            self.db.delete_user(username)

        self.audit(name="user_delete", info={"username": username, "perma": perma})
        self._drop_cache()
    def update_user(self, 
                    username: str, 
                    enable: bool | None = None, 
                    timee: bool | None = None, 
                    wl_enable: bool | None = None) -> None:
        """Disable/enable a user. wl_enable controls specifically the whitelist node.
        Raises a domain error if the user is absent or a panel update fails."""
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
                        raise PanelRejectedError("Panel rejected user update")
            
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
                        raise PanelRejectedError("Panel rejected whitelist user update")

            self.db.update_user(username, status_wl=wl_enable)

            audit_info['wl_enable'] = wl_enable

        self.audit(name="user_update", info=audit_info)
        self._drop_cache()
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
    ) -> NewUserInfo:
        """Adds a new user. Raises a domain error if any argument is incorrect.
        Now also suppports ext username and password (optional)"""
        if ext_username:
            if len(ext_username) > 32:
                raise ValidationError("Ext Username too long")
            ext_username = sanitize(ext_username, "external")
        if token is None:
            token = secrets.token_urlsafe(40)
        if userid is None:
            userid = str(uuid.uuid4())
        else:
            if not isuuid(userid):
                raise ValidationError("Invalid UUID")
        if fingerprint is None:
            fingerprint = random.choice(self.fps)
        else:
            if fingerprint not in self.cfg['fingerprints']:
                raise ValidationError("Invalid fingerprint")
        if timee > 2**31:
            raise ValidationError("Invalid timestamp")
        if ext_password is not None:
            ext_password = self.hash(ext_password)
        else:
            ext_username = None

        
        if len(displayname) > 16:
            raise ValidationError("Displayname too long")
        displayname = sanitize(displayname, "display")
        try:
            self.db.create_user(
                username=username, uuid=userid, token=token, fingerprint=fingerprint,
                displayname=displayname, expires_at=timee, bw_limit_gb=limit,
                wl_limit_gb=wl_limit, ext_username=ext_username,
                ext_password_hash=ext_password,
            )
        except DuplicateError:
            raise ConflictError("Username or external username exists")
        
        try:
            self.add_users(username=username, _called_internally=True)
            return NewUserInfo(
                username=username,
                token=token,
                uuid=userid,
                fingerprint=fingerprint,
                displayname=displayname
            )
        except Exception:
            try:
                self.db.delete_user(username)
            except Exception:
                self.log.error("failed to roll back user %s", username, exc_info=True)
                try:
                    self.delete_user(username=username, perma=True)
                except Exception:
                    self.log.critical(
                        "user %s may be inconsistent after failed DB rollback",
                        username,
                        exc_info=True,
                    )
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
    ) -> None:
        """Updates certain fields for any user. Changing UUIDs isnt supported."""
        current = self._user(username)
        audit_info: dict[str, str | int] = {"username": username}
        if displayname is None:
            displayname = str(current["displayname"])
        else:
            if len(displayname) > 16:
                raise ValidationError("Displayname too long")
            displayname = sanitize(displayname, "display")
            audit_info['displayname'] = displayname
        token = token if token is not None else str(current["token"])
        if fingerprint is None:
            fingerprint = str(current["fingerprint"])
        else:
            audit_info['fingerprint'] = fingerprint
        if fingerprint not in self.fps:
            raise ValidationError("Invalid fingerprint")
        limit = int(current["bw_limit_gb"]) if limit is None else limit
        wl_limit = int(current["wl_limit_gb"]) if wl_limit is None else wl_limit
        timestamp = int(current["expires_at"]) if timee is None else timee
        if timestamp > 2**31:
            raise ValidationError("Invalid time")
        old_ext = current["ext_username"]
        if ext_username is None:
            ext_username = str(old_ext) if old_ext is not None else None
        else:
            if len(ext_username) > 32:
                raise ValidationError("Username too long")
            ext_username = sanitize(ext_username, "external")
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
            raise ConflictError("Ext username exists")

        self.audit(name="user_update_params", info=audit_info)
        self._drop_cache()

    def _mark_rollback_failure(self, username: str, reason: str) -> None:
        try:
            self.db.set_metadata(
                f"uuid_rollback_failed:{username}",
                f"{int(time.time())}:{reason}",
            )
        except Exception:
            self.log.critical(
                "failed to persist UUID rollback failure marker for user %s",
                username,
                exc_info=True,
            )

    def _rollback_user_uuid(
        self,
        username: str,
        old_uid: str,
        new_uid: str,
        successful: list[tuple[XUiSession, int, SettingsClient, bool]],
    ) -> None:
        """Restore old UUID on panels that were already updated before a failure.
        Logs but never raises — rollback failures are logged, not propagated."""
        failures: list[str] = []
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
                    failures.append(f"{panel.name}:{inbound_id}:panel rejected rollback")
                    self.log.critical(
                        f"_rollback_user_uuid: panel {panel.name} inbound {inbound_id} "
                        f"failed to restore UUID {old_uid}: {resp.json().get('msg', 'unknown')}"
                    )
            except Exception as e:
                failures.append(f"{panel.name}:{inbound_id}:exception")
                self.log.critical(
                    f"_rollback_user_uuid: panel {panel.name} inbound {inbound_id} "
                    f"exception during rollback: {e}",
                    exc_info=True,
                )
        if failures:
            self._mark_rollback_failure(username, ",".join(failures))

    def update_uuid(self, username: str, uid: str) -> None:
        """Seperate method for updating the UUID.
        Potentially dangerous operation, seperate function."""
        if not isuuid(uid):
            raise ValidationError("Invalid UUID")
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
                    raise PanelRejectedError("Panel rejected UUID update")

                successful.append((panel, k, the[str(k)], k in need_vision))

        try:
            self.db.update_user(username, uuid=uid)
        except DuplicateError:
            self._rollback_user_uuid(username, olduid, uid, successful)
            raise ConflictError("UUID exists")
        self.audit(name="user_update_uuid", info={"username": username, "uuid": uid})
        self._drop_cache()

    def register_with_code(
        self,
        *,
        code: str,
        username: str,
        displayname: str,
        ext_username: str,
        ext_password: str,
    ) -> RegisterWithCodeInfo:
        """Create a new web user from a register code.
    
        Raises a domain error on validation or business-rule failure.
    
        """
        if not code or not isinstance(code, str):
            raise ValidationError("Invalid code")
        if not username or not isinstance(username, str):
            raise ValidationError("Invalid username")
        if not ext_username or not isinstance(ext_username, str): 
            raise ValidationError("Invalid username")
        if not ext_password or not isinstance(ext_password, str):
            raise ValidationError("Invalid password")

        
        ext_username = sanitize(ext_username, "external")
        if not ext_username:
            raise ValidationError("Invalid username")
        if len(ext_username) > 32:
            raise ValidationError("Ext Username too long")

        
        if len(displayname) > 16:
            raise ValidationError("Displayname too long")
        displayname = sanitize(displayname, "display")
        token = secrets.token_urlsafe(40)
        userid = str(uuid.uuid4())
        fingerprint = random.choice(self.fps)
        hashed_password = self.hash(ext_password)
    
        try:
            result_data = self.db.register_with_code(
                code=code, username=username, uuid=userid, token=token,
                fingerprint=fingerprint, displayname=displayname,
                ext_username=ext_username, ext_password_hash=hashed_password,
            )
        except CodeError as exc:
            raise NotFoundError("Invalid code") from exc
        except DuplicateError as exc:
            raise ConflictError("Username exists") from exc
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
            self.add_users(username=username, _called_internally=True)
        except Exception:
            self.log.critical(f"register_with_code backend sync failed for {username}", exc_info=True)
            self._rollback_registered_user(username=username)
            raise
        self.db.confirm_registration_sync(username)
        self.audit(name="user_add", info=audit_result)
        return result

    def get_code(self, code: str) -> CodeObject:
        """Search for a code. Raises NotFoundError if it does not exist."""

        result = self.db.get_code(code)

        if result is None:
            raise NotFoundError("Unknown code")
        return CodeObject(
            code=code,
            action=result['action'],
            perma=result['perma'],
            uses=result['uses'],
            days=result['days'],
            gb=result['gb'],
            wl_gb=result['wl_gb']
        )
        
    def apply_bonus_code(self, *, username: str, code: str) -> ApplyBonusCodeObject:
        """Atomically validate/consume a bonus code and apply it to a user.
    
        Returns:
            ApplyBonusCodeObject: Applied bonus info on success.
            """
        if not isinstance(code, str) or not code:
            raise ValidationError("Unknown code")
        try:
            result_data = self.db.adjust_bonus(username, code)
        except CodeError as exc:
            if str(exc) == "unknown user":
                raise NotFoundError("Unknown user") from exc
            raise NotFoundError("Unknown code") from exc
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
    ) -> None:
        """
        Creates a code.
        If permanent is True, 'uses' param is ignored.
        """
        
        if not isinstance(code, str) or not code: 
            raise ValidationError("code must be a non-empty string")
        if action not in ("register", "bonus"):
            raise ValidationError(f"action must be 'register' or 'bonus', got '{action}'")
        if not all(isinstance(x, int) for x in (days, gb, wl_gb, uses)): 
            raise ValidationError("days, gb, wl_gb must be integers")
        if days < 0 or gb < 0 or wl_gb < 0:
            raise ValidationError("days, gb, wl_gb must be non-negative")
        if not permanent:
            if uses < 1:
                raise ValidationError("uses must be >= 1")
        
        res: dict[str, str | bool | int] = {
            "code": code, "action": action, "perma": permanent,
            "days": days, "gb": gb, "wl_gb": wl_gb, "uses": uses if not permanent else -1
        }
        try:
            self.db.add_code(code, action, permanent=permanent, days=days, gb=gb, wl_gb=wl_gb, uses=uses)
        except DuplicateError:
            raise ConflictError(f"code '{code}' already exists")
        self.audit(name="code_add", info=res)
    def delete_code(self, code: str) -> None:
        """Delete a code. Raises NotFoundError if it does not exist."""
        deleted = self.db.delete_code(code)
        if not deleted:
            raise NotFoundError("Unknown code")
        self.audit(name="code_delete", info={"code": code})
    def list_code(self) -> list[str]:
        return [str(c['code']) for c in self.db.all_codes() if 'code' in c]
    def bonus_code(self, value: int | str, code: str) -> ApplyBonusCodeObject:
        """Apply a bonus code for a Telegram user."""
        username = self.get_username_telegram(value)
        if not isinstance(username, str) or not username:
            raise NotFoundError("Unknown Telegram user")
        return self.apply_bonus_code(username=username, code=code)

    def get_info(self, username: str, pretty: bool = False) -> UserInfo:
        """Get all info about a user. Raises NotFoundError if it does not exist."""
        
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
    def get_info_telegram(self, tgid: int) -> UserInfo:
        """Returns all user info by telegram ID. Raises NotFoundError if absent."""
        username = self.db.tgid_to_user(tgid)
        if username is None:
            raise NotFoundError("Unknown Telegram user")
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
    def get_online_status(self, new: Literal[False] = False) -> OnlineStatus: ...
    
    @overload
    def get_online_status(self, new: Literal[True]) -> OnlineStatus: ...
    
    @overload
    def get_online_status(self, new: bool) -> OnlineStatus: ...

    def get_online_status(self, new: bool = False) -> OnlineStatus:
        """Get online users and per-panel query health.

        An empty result is valid only when every configured panel reports
        a successful empty response.
        """
        online_users: set[str] = set()
        panel_health: dict[str, Literal["ok", "unavailable", "invalid"]] = {}

        if not self.panels:
            if new:
                return OnlineStatus({}, panel_health)
            return OnlineStatus([], panel_health)

        for panel in self.panels:
            if panel.dead:
                panel_health[panel.name] = "unavailable"
                continue
            try:
                response = panel.post("panel/api/inbounds/onlines")
                data: dict[str, object] = response.json()
                if response.status_code not in (200, 201) or not data.get('success'):
                    panel_health[panel.name] = "unavailable"
                    self.log.error(
                        "Online check failed for panel %s: %s",
                        panel.name,
                        data.get('msg') or response.status_code,
                    )
                    continue
                raw_online = data.get('obj', [])
                if not isinstance(raw_online, list):
                    panel_health[panel.name] = "invalid"
                    self.log.error(
                        "Online check returned invalid payload for panel %s",
                        panel.name,
                    )
                    continue
                raw_emails: list[object] = cast(list[object], raw_online)
                for raw_email in raw_emails:
                    if not isinstance(raw_email, str):
                        panel_health[panel.name] = "invalid"
                        self.log.error(
                            "Online check returned a non-string email for panel %s",
                            panel.name,
                        )
                        break
                    name_candidate = raw_email.rsplit('-', 1)[0]
                    if self.db.user_exists(name_candidate):
                        online_users.add(name_candidate)
                else:
                    panel_health[panel.name] = "ok"
            except Exception as exc:
                panel_health[panel.name] = "unavailable"
                self.log.error("Online check failed for panel %s", panel.name, exc_info=exc)

        if all(health == "unavailable" for health in panel_health.values()):
            raise PanelUnavailableError("No panel could be queried for online users")

        users: list[str] | dict[str, str | None]
        if not new:
            users = list(online_users)
        else:
            users = {name: self.db.user_to_ext(name) for name in online_users}
        return OnlineStatus(users, panel_health)

    @overload
    def get_online_users(self, new: Literal[False] = False) -> list[str]: ...

    @overload
    def get_online_users(self, new: Literal[True]) -> dict[str, str | None]: ...

    @overload
    def get_online_users(self, new: bool) -> list[str] | dict[str, str | None]: ...

    def get_online_users(self, new: bool = False) -> list[str] | dict[str, str | None]:
        """Compatibility wrapper for callers that do not need panel health."""
        return self.get_online_status(new).users

    def is_online(self, username: str) -> bool:
        """Return True only when the user is online and every panel is healthy.

        No configured panels is an explicit known-empty state rather than an
        availability failure.
        """
        if not self.panels:
            return False
        status = self.get_online_status()
        return (
            all(health == "ok" for health in status.panel_health.values())
            and username in status.users
        )

    def reset_user(self, username: str) -> ResetUserObject:
        """Resets token and uuid to randomness. Dict with new values on success."""
        newid = str(uuid.uuid4())
        newt = secrets.token_urlsafe(40)
        self.update_uuid(username, newid)
        self.update_params(
            username=username,
            token=newt
        )
        self.set_auth_token(username, None)
        self.audit(name="user_reset", info={"username": username, "uuid": newid, "token": "redacted"})
        self._drop_cache()
        return ResetUserObject(uuid=newid, token=newt)
