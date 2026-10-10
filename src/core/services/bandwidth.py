import time
from concurrent.futures import Executor

from dacite import Config as DConfig
from dacite import from_dict

from custom_types import BandwidthInfo, BandwidthSnapshot, PanelClient
from errors import AppError, PanelUnavailableError
from session import XUiSession
from sysutil import StateSnapshot
from tracer import Op

from ..common import BaseService, SharedCoreResources
from .panel import PanelService
from .user.common import CommonUserService

__all__ = ["BandwidthService"]

class BandwidthService(BaseService):
    def __init__(
        self,
        res: SharedCoreResources,
        *,
        panel_svc: PanelService,
        user_svc: CommonUserService
    ) -> None:
        super().__init__(res)
        self.panel_svc: PanelService = panel_svc
        self.user_svc: CommonUserService = user_svc

    def _target_panels(self, whitelist: bool) -> list[XUiSession]:
        """Panels a bandwidth read applies to: the whitelist slot or the rest."""
        if whitelist:
            # No whitelist panel configured -> there is nothing to report.
            # NOTE: don't return a bare `[None]` list here, panel helpers
            # dereference attributes on the panel immediately.
            if not self.whitelist_panel:
                return []
            return [self.whitelist_panel]
        return [p for p in self.panels if p != self.whitelist_panel]

    def bandwidth(
        self,
        username: str,
        whitelist: bool = False,
        *,
        pool: Executor | None = None,
    ) -> BandwidthInfo:
        """Get bandwidth info about a user. Returns BandwidthInfo(upload, download, total). In bytes.

        The clients-first API keeps ONE shared ``client_traffics`` row per
        panel client — reading it per panel is the whole answer. Never sum
        it across inbounds; that double-counts. Cross-PANEL summation stays
        (a user is one client per panel).
        """
        self.user_svc.user(username)  # keeps the NotFoundError contract
        self.trace(
            Op.bandwidth.bandwidth, "start",
            username=username, whitelist=whitelist,
            pool=type(pool).__name__ if pool is not None else None,
        )

        up_total = 0
        down_total = 0
        panels = self._target_panels(whitelist)
        # First failing panel in list order still raises. Sibling requests
        # may finish in the background; they are not cancelled.
        rows = self.panel_svc.map_panels(
            panels,
            lambda panel: self.panel_svc.client_traffic(panel, username),
            pool,
        )
        found = 0
        for traffic in rows:
            if traffic is not None:
                found += 1
                up_total += traffic.up
                down_total += traffic.down

        self.trace(
            Op.bandwidth.bandwidth, "ok",
            username=username, whitelist=whitelist,
            panels=[panel.name for panel in panels],
            found=found, absent=len(rows) - found,
            upload=up_total, download=down_total, total=up_total + down_total,
        )
        return BandwidthInfo(up_total, down_total, up_total + down_total)

    def all_traffic(
        self,
        whitelist: bool = False,
        *,
        pool: Executor | None = None,
    ) -> dict[str, BandwidthInfo]:
        """Batch path for pollers: one ``clients/list`` per target panel.

        Reduces every panel client into ``email -> BandwidthInfo`` summed
        across panels (never across a client's inbounds). Emails that are
        not known usernames are skipped. Raises ``PanelUnavailableError``
        when no target panel could be queried — the same rule
        ``get_online_status`` uses.
        """
        panels = self._target_panels(whitelist)
        self.trace(
            Op.bandwidth.all_traffic, "start",
            whitelist=whitelist,
            panels=[panel.name for panel in panels],
            pool=type(pool).__name__ if pool is not None else None,
        )
        if not panels:
            self.trace(Op.bandwidth.all_traffic, "empty", whitelist=whitelist, users=0)
            return {}
        totals: dict[str, list[int]] = {}
        queried = 0
        known = self.db.usernames()

        def fetch(panel: XUiSession) -> list[PanelClient] | AppError:
            try:
                return self.panel_svc.list_clients(panel)
            except AppError as exc:
                return exc

        # A raising map would skip a live later panel when the first fails.
        for panel, outcome in zip(
            panels, self.panel_svc.map_panels(panels, fetch, pool), strict=True,
        ):
            if isinstance(outcome, AppError):
                self.log.error(
                    "client traffic listing failed for panel %s",
                    panel.name, exc_info=outcome,
                )
                continue
            clients = outcome
            queried += 1
            for client in clients:
                traffic = client.traffic
                if traffic is None or client.email not in known:
                    continue
                acc = totals.setdefault(client.email, [0, 0])
                acc[0] += traffic.up
                acc[1] += traffic.down
        if queried == 0:
            self.trace(
                Op.bandwidth.all_traffic, "unavailable",
                whitelist=whitelist, panels=len(panels),
            )
            raise PanelUnavailableError("No panel could be queried for client traffic")
        result = {
            email: BandwidthInfo(up, down, up + down)
            for email, (up, down) in totals.items()
        }
        self.trace(
            Op.bandwidth.all_traffic, "ok",
            whitelist=whitelist, queried=queried, panels=len(panels),
            users=len(result),
        )
        return result

    def get_bw_history(self, username: str, days: int = 30) -> list[BandwidthSnapshot]:
        """Return snapshots for a user, clamped to retention window."""
        cutoff = int(time.time()) - days * 86400
        return [BandwidthSnapshot(**row) for row in self.db.get_bandwidth_snapshots(username, cutoff)]

    def get_snapshots(self, days: int = 30) -> list[StateSnapshot]:
        """Return state snapshots, clamped to a retention window."""
        cutoff = int(time.time()) - days * 86400
        return [from_dict(StateSnapshot, row, config=DConfig(cast=[tuple]))
                for row in self.db.get_state_snapshots(cutoff)]
