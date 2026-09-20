# type: ignore
# pyright: ignore

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

    def bonus_code(self, value: int | str, code: str) -> ApplyBonusCodeObject:
        """Apply a bonus code for a Telegram user."""
        username = self.get_username_telegram(value)
        if not isinstance(username, str) or not username:
            raise NotFoundError("Unknown Telegram user")
        return self.apply_bonus_code(username=username, code=code)




