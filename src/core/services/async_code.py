from __future__ import annotations

import random
import time
import uuid
from dataclasses import asdict
from typing import TYPE_CHECKING, Literal

from custom_types import ApplyBonusCodeObject, CodeObject, RegisterWithCodeInfo
from errors import CodeError, ConflictError, DuplicateError, NotFoundError, ValidationError
from util import generate_token, isusername, sanitize

from ..async_common import AsyncBaseService
from ..coordination import user_mutation

# pyright: reportUnnecessaryIsInstance=false

if TYPE_CHECKING:
    from ..async_common import AsyncCoreResources
    from .async_audit import AsyncAuditService
    from .async_password import AsyncPasswordService
    from .async_user import AsyncBusinessUserService


class AsyncCodeService(AsyncBaseService):
    def __init__(self, res: AsyncCoreResources, *, audit_svc: AsyncAuditService) -> None:
        super().__init__(res)
        self.audit_svc = audit_svc

    async def get_code(self, code: str) -> CodeObject:
        result = await self.db.get_code(code)
        if result is None:
            raise NotFoundError("Unknown code")
        return CodeObject(code=code, action=result["action"], perma=result["perma"], uses=result["uses"], days=result["days"], gb=result["gb"], wl_gb=result["wl_gb"])

    @user_mutation
    async def apply_bonus_code(self, *, username: str, code: str) -> ApplyBonusCodeObject:
        if not isinstance(code, str) or not code:
            raise ValidationError("Unknown code")
        try:
            data = await self.db.adjust_bonus(username, code)
        except CodeError as exc:
            raise NotFoundError("Unknown user" if str(exc) == "unknown user" else "Unknown code") from exc
        result = ApplyBonusCodeObject(**data)
        await self.audit_svc.audit(name="user_consume_code", info=asdict(result))
        return result

    async def add_code(self, code: str, action: str, permanent: bool = False, days: int = 0, gb: int = 0, wl_gb: int = 0, uses: int = 1) -> None:
        if not isinstance(code, str) or not code:
            raise ValidationError("code must be a non-empty string")
        if action not in {"register", "bonus"}:
            raise ValidationError("action must be 'register' or 'bonus'")
        if not all(isinstance(value, int) for value in (days, gb, wl_gb, uses)) or min(days, gb, wl_gb) < 0:
            raise ValidationError("days, gb, wl_gb must be non-negative integers")
        if not permanent and uses < 1:
            raise ValidationError("uses must be >= 1")
        try:
            await self.db.add_code(code, action, permanent=permanent, days=days, gb=gb, wl_gb=wl_gb, uses=uses)
        except DuplicateError as exc:
            raise ConflictError(f"code '{code}' already exists") from exc
        await self.audit_svc.audit(name="code_add", info={"code": code, "action": action, "perma": permanent, "days": days, "gb": gb, "wl_gb": wl_gb, "uses": -1 if permanent else uses})

    async def delete_code(self, code: str) -> None:
        if not await self.db.delete_code(code):
            raise NotFoundError("Unknown code")
        await self.audit_svc.audit(name="code_delete", info={"code": code})

    async def list_code(self) -> list[str]:
        return [row["code"] for row in await self.db.all_codes()]


class AsyncBusinessCodeService(AsyncBaseService):
    def __init__(self, res: AsyncCoreResources, *, audit_svc: AsyncAuditService, password_svc: AsyncPasswordService, user_svc: AsyncBusinessUserService) -> None:
        super().__init__(res)
        self.audit_svc = audit_svc
        self.password_svc = password_svc
        self.user_svc = user_svc

    @user_mutation
    async def _rollback_registered_user(self, *, username: str) -> None:
        key = f"registration_rollback_failed:{username}"
        await self.db.set_metadata(key, str(int(time.time())))
        user = await self.db.get_user(username)
        if user is not None:
            await self.user_svc.rollback_panel_clients(username, expected_uuid=user["uuid"])
        # Never refund or erase pending identity until every panel is clean.
        await self.db.rollback_registration_sync(username)
        await self.db.delete_metadata(key)

    @user_mutation
    async def register_with_code(self, *, code: str, username: str, displayname: str, ext_username: str, ext_password: str) -> RegisterWithCodeInfo:
        if not isinstance(code, str) or not code:
            raise ValidationError("Invalid code")
        if not isinstance(username, str) or not isusername(username):
            raise ValidationError("Invalid username")
        if not isinstance(ext_username, str) or not isinstance(ext_password, str) or not ext_username or not ext_password:
            raise ValidationError("Invalid credentials")
        await self.user_svc.require_recovered(username)
        external = sanitize(ext_username, "external")
        if not external or len(external) > 32:
            raise ValidationError("Invalid username")
        if len(displayname) > 16:
            raise ValidationError("Displayname too long")
        token, uid = generate_token("sub"), str(uuid.uuid4())
        fingerprint = random.choice(self.cfg.view()["fingerprints"])
        hashed = await self.password_svc.hash(ext_password)
        try:
            data = await self.db.register_with_code(code=code, username=username, uuid=uid, token=token, fingerprint=fingerprint, displayname=sanitize(displayname, "display"), ext_username=external, ext_password_hash=hashed)
        except CodeError as exc:
            raise NotFoundError("Invalid code") from exc
        except DuplicateError as exc:
            raise ConflictError("Username exists") from exc
        try:
            await self.user_svc.add_users(username, _called_internally=True)
            await self.db.confirm_registration_sync(username)
        except Exception:
            try:
                await self._rollback_registered_user(username=username)
            except Exception:
                self.log.critical("registration panel rollback failed for %s; pending identity retained", username, exc_info=True)
            raise
        result = RegisterWithCodeInfo(username=username, token=token, uuid=uid, fingerprint=fingerprint, limit=data["gb"], wl_limit=data["wl_gb"], time=data["time"])
        await self.audit_svc.audit(name="user_add", info={"username": username, "ext_username": external, "uuid": uid, "fingerprint": fingerprint, "limit": result.limit, "wl_limit": result.wl_limit, "time": result.time})
        return result

    async def recover_rollback_failures(self) -> None:
        # Pending rows cover a crash before a failure marker could be written.
        users = set(await self.db.pending_registrations())
        users.update(key.removeprefix("registration_rollback_failed:") for key in await self.db.list_metadata("registration_rollback_failed:"))
        for username in sorted(users):
            try:
                await self._rollback_registered_user(username=username)
            except Exception:
                self.log.critical("registration rollback recovery failed for %s; marker retained", username, exc_info=True)

    async def get_rollback_failures(self) -> dict[str, dict[str, dict[str, str]]]:
        result: dict[str, dict[str, dict[str, str]]] = {"uuid": {}, "registration": {}}
        for kind in ("uuid", "registration"):
            prefix = f"{kind}_rollback_failed:"
            for key, value in (await self.db.list_metadata(prefix)).items():
                timestamp, _, reason = value.partition(":")
                result[kind][key.removeprefix(prefix)] = {"ts": timestamp, "reason": reason}
        return result

    @user_mutation
    async def clear_rollback_failure(self, kind: Literal["uuid", "registration"], username: str) -> None:
        if kind not in {"uuid", "registration"}:
            raise ValidationError("Invalid rollback marker kind")
        await self.db.delete_metadata(f"{kind}_rollback_failed:{username}")
