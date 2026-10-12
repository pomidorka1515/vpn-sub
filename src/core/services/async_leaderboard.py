from __future__ import annotations

import operator
from typing import TYPE_CHECKING, Literal

from ..async_common import AsyncBaseService

if TYPE_CHECKING:
    from ..async_common import AsyncCoreResources
    from .async_bandwidth import AsyncBandwidthService
    from .async_user import AsyncCommonUserService


class AsyncLeaderboardService(AsyncBaseService):
    def __init__(self, res: AsyncCoreResources, *, user_svc: AsyncCommonUserService, bandwidth_svc: AsyncBandwidthService) -> None:
        super().__init__(res)
        self.user_svc = user_svc
        self.bandwidth_svc = bandwidth_svc

    async def leaderboard(self, category: Literal["total", "monthly", "wl_monthly"], *, top_n: int = 0, use_displaynames: bool = False, flip: bool = False) -> dict[str, int]:
        records = await self.user_svc.list_user_states()
        totals = await self.bandwidth_svc.all_traffic(work="background") if category == "total" else {}
        raw: dict[str, int] = {}
        for record in records:
            name = record["displayname"] if use_displaynames else record["username"]
            if category == "total":
                info = totals.get(record["username"])
                raw[name] = int(info.total) if info is not None else 0
            elif category == "monthly" and record["bw_limit_gb"] != 0:
                raw[name] = record["bw_used"]
            elif category == "wl_monthly" and record["wl_limit_gb"] != 0:
                raw[name] = record["wl_used"]
        ranked = sorted(raw.items(), key=operator.itemgetter(1), reverse=not flip)
        return dict(ranked[:top_n] if top_n > 0 else ranked)
