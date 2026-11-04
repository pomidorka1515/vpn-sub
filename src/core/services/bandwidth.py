from __future__ import annotations

import time
from typing import TYPE_CHECKING

from dacite import Config as DConfig
from dacite import from_dict

from custom_types import BandwidthInfo, BandwidthSnapshot
from errors import PanelUnavailableError
from sysutil import StateSnapshot

from ..common import BaseService

if TYPE_CHECKING:
    from session.client import XUiSession

    from ..common import SharedCoreResources
    from .panel import PanelService, WorkClass
    from .user import CommonUserService


class BandwidthService(BaseService):
    def __init__(
        self,
        res: SharedCoreResources,
        *,
        panel_svc: PanelService,
        user_svc: CommonUserService,
    ) -> None:
        super().__init__(res)
        self.panel_svc = panel_svc
        self.user_svc = user_svc

    def _target_panels(self, whitelist: bool) -> list[XUiSession]:
        if whitelist:
            return [self.whitelist_panel] if self.whitelist_panel else []
        return [panel for panel in self.panels if panel != self.whitelist_panel]

    async def bandwidth(
        self,
        username: str,
        whitelist: bool = False,
        *,
        work: WorkClass = "foreground",
    ) -> BandwidthInfo:
        self.require_panels()
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
        self,
        whitelist: bool = False,
        *,
        work: WorkClass = "foreground",
    ) -> dict[str, BandwidthInfo]:
        panels = await self.panel_traffic(whitelist, work=work)
        totals: dict[str, BandwidthInfo] = {}
        for values in panels.values():
            for email, traffic in values.items():
                old = totals.get(email, BandwidthInfo(0, 0, 0))
                totals[email] = BandwidthInfo(
                    old.upload + traffic.upload,
                    old.download + traffic.download,
                    old.total + traffic.total,
                )
        return totals

    async def panel_traffic(
        self,
        whitelist: bool = False,
        *,
        work: WorkClass = "foreground",
    ) -> dict[str, dict[str, BandwidthInfo]]:
        """Return counters without merging independent panel reset domains."""
        self.require_panels()
        panels = self._target_panels(whitelist)
        if not panels:
            return {}
        known = await self.db.usernames()
        outcomes = await self.panel_svc.outcomes(panels, self.panel_svc.list_clients, work=work)
        result: dict[str, dict[str, BandwidthInfo]] = {}
        for outcome in outcomes:
            if outcome.error is not None:
                self.log.error("client traffic listing failed for panel %s", outcome.panel)
                raise PanelUnavailableError(
                    f"Could not query client traffic for panel {outcome.panel}",
                ) from outcome.error
            values = result[outcome.panel] = {}
            for client in outcome.value or []:
                traffic = client.traffic
                if traffic is None or client.email not in known:
                    continue
                values[client.email] = BandwidthInfo(traffic.up, traffic.down, traffic.up + traffic.down)
        return result

    async def get_bw_history(self, username: str, days: int = 30) -> list[BandwidthSnapshot]:
        return [
            BandwidthSnapshot(**row)
            for row in await self.db.get_bandwidth_snapshots(
                username,
                int(time.time()) - days * 86400,
            )
        ]

    async def get_snapshots(self, days: int = 30) -> list[StateSnapshot]:
        return [
            from_dict(StateSnapshot, row, config=DConfig(cast=[tuple]))
            for row in await self.db.get_state_snapshots(int(time.time()) - days * 86400)
        ]
