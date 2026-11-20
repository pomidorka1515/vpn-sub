from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from dacite import Config as DConfig
from dacite import from_dict

from custom_types import BandwidthInfo, BandwidthSnapshot
from errors import PanelUnavailableError
from sysutil import StateSnapshot

from ..common import BaseService

if TYPE_CHECKING:
    from collections.abc import Mapping

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
        """Omit missing traffic and rows whose email or UUID no longer matches the user.

        Identity is sampled after panel I/O, without taking a user mutation lock:
        callers may already hold that lock while gathering this read in child tasks.
        """
        self.require_panels()
        await self.user_svc.user(username)
        rows = await self.panel_svc.map_panels(
            self._target_panels(whitelist),
            lambda panel: self.panel_svc.client_traffic(panel, username),
            work=work,
        )
        user = await self.db.get_user(username)
        owned = [
            row
            for row in rows
            if user is not None
            and row is not None
            and row.email == username
            and row.uuid == user["uuid"]
        ]
        up = sum(row.up for row in owned)
        down = sum(row.down for row in owned)
        return BandwidthInfo(up, down, up + down)

    async def all_traffic(
        self,
        whitelist: bool = False,
        *,
        work: WorkClass = "foreground",
    ) -> dict[str, BandwidthInfo]:
        panels = await self.panel_traffic(whitelist, work=work, require_complete=True)
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

    async def subscription_bandwidth(self, username: str, uuid: str) -> BandwidthInfo:
        """Display-only counters filtered against the subscription's user snapshot."""
        self.require_panels()
        rows = await asyncio.gather(
            *(
                self.panel_svc.subscription_traffic(panel, username)
                for panel in self._target_panels(False)
            )
        )
        owned = [
            row
            for row in rows
            if row is not None and row[0] == row[2] == username and row[1] == row[3] == uuid
        ]
        up = sum(row[4] for row in owned)
        down = sum(row[5] for row in owned)
        return BandwidthInfo(up, down, up + down)

    async def panel_traffic(
        self,
        whitelist: bool = False,
        *,
        work: WorkClass = "foreground",
        require_complete: bool = False,
        known: Mapping[str, str] | None = None,
    ) -> dict[str, dict[str, BandwidthInfo]]:
        """Return independent counters, omitting unavailable panels unless completeness is required.

        Omitted panels must keep their previous accounting baselines, not advance
        to zero. Aggregate consumers require a complete sample instead. Stale
        samples cannot be used as accounting baselines across a rollover.
        """
        self.require_panels()
        panels = self._target_panels(whitelist)
        if not panels:
            return {}
        if known is None:
            known = {user["username"]: user["uuid"] for user in await self.db.list_sweep_users()}
        snapshots = await asyncio.gather(
            *(
                self.panel_svc.traffic_snapshot(panel, work=work, allow_stale=False)
                for panel in panels
            ),
            return_exceptions=True,
        )
        result: dict[str, dict[str, BandwidthInfo]] = {}
        for panel, snapshot in zip(panels, snapshots, strict=True):
            if isinstance(snapshot, BaseException):
                if not isinstance(snapshot, Exception):
                    raise snapshot from None
                self.log.error("client traffic listing failed for panel %s", panel.name)
                if require_complete:
                    raise PanelUnavailableError(
                        f"Could not query client traffic for panel {panel.name}",
                    ) from snapshot
                continue
            values = result[panel.name] = {}
            for email, uuid, _traffic_email, _traffic_uuid, up, down in snapshot.rows:
                if known.get(email) != uuid:
                    continue
                values[email] = BandwidthInfo(up, down, up + down)
        return result

    async def get_bw_history(self, username: str, days: int = 30) -> list[BandwidthSnapshot]:
        """Return end-filtered interval deltas, not exact rolling-period totals.

        Starts may precede the cutoff or be unknown for retained legacy records
        and aggregates whose independent counters have incompatible baselines.
        """
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
