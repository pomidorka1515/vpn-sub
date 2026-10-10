from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal

from config import JsonValue

from ..common import BaseService

__all__ = ["AUDIT_VALUES", "AuditService"]

AUDIT_VALUES = Literal[
    'sub_hit',
    'user_refresh', 'user_delete', 'user_reset',
    'user_update', 'user_update_params', 'user_add',
    'user_update_uuid', 'user_consume_code',
    'code_add', 'code_delete',
    'config_update',

]

class AuditService(BaseService):
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
        ts = datetime.now(UTC)
        cur_date = ts.strftime("%d.%m.%Y %H:%M:%S")
        to_log: Mapping[str, str | int | float | Mapping[str, JsonValue] | None] = {
            "ts": ts.timestamp(),
            "date": cur_date,
            "action": name,
            "info": info
        }
        self.audit_cfg.append(record=to_log)
