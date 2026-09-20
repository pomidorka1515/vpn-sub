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
from util import isuuid, compare, sanitize, generate_token
from dataclasses import asdict
from collections.abc import Mapping

# pyright: reportUnnecessaryIsInstance=false

__all__ = [
    "Subscription",
    "OnlineStatus"
]

if platform.system().lower() != 'linux': # avoid pylance static evaluation
    raise UnsupportedPlatformError()


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
            self.uri: str = cfg['uri'].strip("/")
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
        token = generate_token("sub")
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

