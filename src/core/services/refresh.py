from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from errors import PanelUnavailableError

if TYPE_CHECKING:
    from core import Subscription


@dataclass
class RefreshResult:
    failed: list[str]
    aborted: str | None
    succeeded: int
    total: int


class RefreshService:
    def __init__(self, sub: Subscription) -> None:
        self.sub = sub

    async def refresh(self) -> RefreshResult:
        users = await self.sub.user_svc.list_users()
        pending = iter(users)
        failed: set[str] = set()
        aborted: str | None = None
        succeeded = 0

        async def worker() -> None:
            nonlocal aborted, succeeded
            for username in pending:
                if aborted is not None:
                    return
                try:
                    await self.sub.business_svc.add_users(username)
                except PanelUnavailableError:
                    failed.add(username)
                    self.sub.res.log.exception("user refresh failed for %s", username)
                except Exception:
                    if aborted is None:
                        aborted = username
                    self.sub.res.log.critical(
                        "bulk user refresh aborted for %s", username, exc_info=True,
                    )
                else:
                    succeeded += 1

        # Drain in-flight mutations on abort rather than cancelling them midway.
        async with asyncio.TaskGroup() as group:
            for index in range(min(4, len(users))):
                group.create_task(worker(), name=f"core:user-refresh-worker:{index}")
        return RefreshResult(
            [name for name in users if name in failed], aborted, succeeded, len(users),
        )
