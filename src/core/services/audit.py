from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from config import JsonValue
    from config.store import AsyncLinesConfig


class AuditService:
    """Return only after durable append; failures propagate to the caller."""

    def __init__(self, store: AsyncLinesConfig | None) -> None:
        self._store = store

    async def audit(
        self, *, name: AUDIT_VALUES, info: Mapping[str, JsonValue] | None = None,
    ) -> None:
        if self._store is None:
            return
        now = datetime.now(UTC)
        await self._store.append(
            {
                "ts": now.timestamp(),
                "date": now.strftime("%d.%m.%Y %H:%M:%S"),
                "action": name,
                "info": copy.deepcopy(dict(info)) if info is not None else None,
            },
        )


type AUDIT_VALUES = Literal[
    "sub_hit", "user_refresh", "user_delete", "user_reset", "user_update",
    "user_update_params", "user_add", "user_update_uuid", "user_consume_code",
    "code_add", "code_delete", "config_update",
]
