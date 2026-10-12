from __future__ import annotations

import time
from typing import TYPE_CHECKING

from dacite import Config as DConfig
from dacite import from_dict

from custom_types import BandwidthInfo, BandwidthSnapshot
from errors import PanelUnavailableError
from sysutil import StateSnapshot

from ..async_common import AsyncBaseService

if TYPE_CHECKING:
    from session.async_client import AsyncXUiSession

    from ..async_common import AsyncCoreResources
    from .async_panel import AsyncPanelService, WorkClass
    from .async_user import AsyncCommonUserService


class AsyncBandwidthService(AsyncBaseService):
    def __init__(
        self,
        res: AsyncCoreResources,
        *,
        panel_svc: AsyncPanelService,
        user_svc: AsyncCommonUserService,
    ) -> None:
        super().__init__(res)
        self.panel_svc = panel_svc
        self.user_svc = user_svc

    def _target_panels(self, whitelist: bool) -> list[AsyncXUiSession]:
        if whitelist:
            return [self.whitelist_panel] if self.whitelist_panel else []
        return [panel for panel in self.panels if panel != self.whitelist_panel]

    async def bandwidth(
        self, username: str, whitelist: bool = False, *, work: WorkClass = "foreground",
    ) -> BandwidthInfo:
        await self.user_svc.user(username)
        rows = await self.panel_svc.map_panels(
            self._target_panels(whitelist),
            lambda panel: self.panel_svc.client_traffic(panel, username),
            work=work,
        )
        up = sum(row.up for row in rows if row is not None)
        down = sum(row.down for row in rows if row is not None)
        return BandwidthInfo(up, down, up + down)

    async def all_traffic(
        self, whitelist: bool = False, *, work: WorkClass = "foreground",
    ) -> dict[str, BandwidthInfo]:
        panels = self._target_panels(whitelist)
        if not panels:
            return {}
        known = await self.db.usernames()
        outcomes = await self.panel_svc.outcomes(panels, self.panel_svc.list_clients, work=work)
        totals: dict[str, list[int]] = {}
        queried = 0
        for outcome in outcomes:
            if outcome.error is not None:
                self.log.error("client traffic listing failed for panel %s", outcome.panel)
                continue
            queried += 1
            for client in outcome.value or []:
                traffic = client.traffic
                if traffic is None or client.email not in known:
                    continue
                acc = totals.setdefault(client.email, [0, 0])
                acc[0] += traffic.up
                acc[1] += traffic.down
        if not queried:
            raise PanelUnavailableError("No panel could be queried for client traffic")
        return {email: BandwidthInfo(up, down, up + down) for email, (up, down) in totals.items()}

    async def get_bw_history(self, username: str, days: int = 30) -> list[BandwidthSnapshot]:
        return [
            BandwidthSnapshot(**row)
            for row in await self.db.get_bandwidth_snapshots(
                username, int(time.time()) - days * 86400,
            )
        ]

    async def get_snapshots(self, days: int = 30) -> list[StateSnapshot]:
        return [
            from_dict(StateSnapshot, row, config=DConfig(cast=[tuple]))
            for row in await self.db.get_state_snapshots(int(time.time()) - days * 86400)
        ]
