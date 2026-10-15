from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from errors import PanelUnavailableError

from ..common import BaseService

if TYPE_CHECKING:
    from ..common import SharedCoreResources
    from .user import BusinessUserService, CommonUserService


@dataclass
class RefreshResult:
    failed: list[str]
    aborted: str | None
    succeeded: int
    total: int


class RefreshService(BaseService):
    def __init__(
        self,
        res: SharedCoreResources,
        *,
        user_svc: CommonUserService,
        business_svc: BusinessUserService,
    ) -> None:
        super().__init__(res)
        self.user_svc = user_svc
        self.business_svc = business_svc

    async def refresh(self) -> RefreshResult:
        self.require_panels()
        users = await self.user_svc.list_users()
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
                    await self.business_svc.add_users(username)
                except PanelUnavailableError:
                    failed.add(username)
                    self.log.exception("user refresh failed for %s", username)
                except Exception:
                    if aborted is None:
                        aborted = username
                    self.log.critical(
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
