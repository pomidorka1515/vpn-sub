from collections.abc import Mapping
from typing import Literal

import random
import uuid
import time

from ...common import BaseService, SharedCoreResources
from ..password import PasswordService
from ..audit import AuditService
from ..user.business import BusinessUserService
from errors import DuplicateError, NotFoundError, CodeError, ConflictError, ValidationError
from custom_types import RegisterWithCodeInfo
from util import generate_token, isusername, sanitize

# pyright: reportUnnecessaryIsInstance=false

__all__ = ["BusinessCodeService"]

class BusinessCodeService(BaseService):
    def __init__(
        self,
        res: SharedCoreResources,
        *,
        audit_svc: AuditService,
        password_svc: PasswordService,
        user_svc: BusinessUserService
    ) -> None:
        super().__init__(res)
        self.audit_svc: AuditService = audit_svc
        self.password_svc: PasswordService = password_svc
        self.user_svc: BusinessUserService = user_svc
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
        if not isusername(username):
            # the username becomes the panel client email (clients-first API
            # join key) — the panel rejects emails with whitespace/slashes
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
        fingerprint = random.choice(self.cfg['fingerprints'])
        hashed_password = self.password_svc.hash(ext_password)

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
            self.user_svc.add_users(username=username, _called_internally=True)
        except Exception:
            self.log.critical(f"register_with_code backend sync failed for {username}", exc_info=True)
            self._rollback_registered_user(username=username)
            raise
        self.db.confirm_registration_sync(username)
        self.audit_svc.audit(name="user_add", info=audit_result)
        return result
