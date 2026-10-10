import random
import time
import uuid
from collections.abc import Mapping
from typing import Literal

from custom_types import RegisterWithCodeInfo
from errors import (
    CodeError,
    ConflictError,
    DuplicateError,
    NotFoundError,
    ValidationError,
)
from tracer import Op
from util import generate_token, isusername, sanitize

from ...common import BaseService, SharedCoreResources
from ..audit import AuditService
from ..password import PasswordService
from ..user.business import BusinessUserService

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
        self.trace(Op.register.rollback_registered_user, "start", username=username)
        # Registration is committed before the panel request so panel I/O is
        # never inside a SQLite transaction. The pending record makes removal
        # plus a finite-code refund one short local transaction.
        try:
            self.db.rollback_registration_sync(username)
            self.trace(Op.register.rollback_registered_user, "rolled_back", username=username)
        except Exception:
            self.trace(Op.register.rollback_registered_user, "failed", username=username)
            self.log.critical(
                "register rollback failed for user %s; pending registration may remain",
                username,
                exc_info=True,
            )
            try:
                self.db.set_metadata(f"registration_rollback_failed:{username}", str(int(time.time())))
                self.trace(
                    Op.register.rollback_registered_user, "marker_persisted",
                    username=username,
                )
            except Exception:
                self.trace(
                    Op.register.rollback_registered_user, "marker_failed",
                    username=username,
                )
                self.log.critical(
                    "failed to persist registration rollback failure marker for user %s",
                    username,
                    exc_info=True,
                )

    def recover_rollback_failures(self) -> None:
        """Retry persisted registration rollbacks at startup."""
        prefix = "registration_rollback_failed:"
        keys = tuple(self.db.list_metadata(prefix))
        self.trace(Op.register.recover_rollback_failures, "start", markers=len(keys))
        recovered = 0
        retained = 0
        for key in keys:
            username = key.removeprefix(prefix)
            try:
                self.db.rollback_registration_sync(username)
                self.db.delete_metadata(key)
                recovered += 1
                self.log.warning("recovered registration rollback marker for user %s", username)
            except Exception:
                retained += 1
                self.log.critical(
                    "registration rollback recovery failed for user %s; marker retained",
                    username,
                    exc_info=True,
                )
        self.trace(
            Op.register.recover_rollback_failures, "done",
            markers=len(keys), recovered=recovered, retained=retained,
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
        self.trace(
            Op.register.clear_rollback_failure, "start",
            kind=kind, username=username,
        )
        self.db.delete_metadata(f"{kind}_rollback_failed:{username}")
        self.trace(
            Op.register.clear_rollback_failure, "cleared",
            kind=kind, username=username,
        )

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

        self.trace(
            Op.register.register_with_code, "start",
            code=code, username=username, displayname=displayname,
            ext_username=ext_username,
        )

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
        conf = self.cfg.view()
        fingerprint = random.choice(conf['fingerprints'])
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
        self.trace(
            Op.register.register_with_code, "registered",
            code=code, username=username, uuid=userid, fingerprint=fingerprint,
            displayname=displayname, ext_username=ext_username,
            limit=result.limit, wl_limit=result.wl_limit, time=result.time,
        )
        self.audit_svc.audit(name="user_add", info=audit_result)
        return result
