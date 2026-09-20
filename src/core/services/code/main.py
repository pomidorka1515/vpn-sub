from dataclasses import asdict

from ...common import BaseService, SharedCoreResources
from ..audit import AuditService
from custom_types import CodeObject, ApplyBonusCodeObject
from errors import CodeError, ConflictError, ValidationError, DuplicateError, NotFoundError

# pyright: reportUnnecessaryIsInstance=false

__all__ = ["CodeService"]

class CodeService(BaseService):
    def __init__(
        self,
        res: SharedCoreResources,
        *,
        audit_svc: AuditService
    ) -> None:
        super().__init__(res)
        self.audit_svc: AuditService = audit_svc
    
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
        self.audit_svc.audit(name="user_consume_code", info=asdict(result))
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
        self.audit_svc.audit(name="code_add", info=res)
    def delete_code(self, code: str) -> None:
        """Delete a code. Raises NotFoundError if it does not exist."""
        deleted = self.db.delete_code(code)
        if not deleted:
            raise NotFoundError("Unknown code")
        self.audit_svc.audit(name="code_delete", info={"code": code})
    def list_code(self) -> list[str]:
        return [str(c['code']) for c in self.db.all_codes() if 'code' in c]
