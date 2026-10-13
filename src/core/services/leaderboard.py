from __future__ import annotations

import operator
from typing import TYPE_CHECKING, Literal

from ..common import BaseService

if TYPE_CHECKING:
    from ..common import SharedCoreResources
    from .bandwidth import BandwidthService
    from .user import CommonUserService


class LeaderboardService(BaseService):
    def __init__(
        self,
        res: SharedCoreResources,
        *,
        user_svc: CommonUserService,
        bandwidth_svc: BandwidthService,
    ) -> None:
        super().__init__(res)
        self.user_svc = user_svc
        self.bandwidth_svc = bandwidth_svc

    async def leaderboard(
        self,
        category: Literal["total", "monthly", "wl_monthly"],
        *,
        top_n: int = 0,
        use_displaynames: bool = False,
        flip: bool = False,
    ) -> dict[str, int]:
        records = await self.user_svc.list_user_states()
        totals = (
            await self.bandwidth_svc.all_traffic(work="background") if category == "total" else {}
        )
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
