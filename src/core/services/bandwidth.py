import time
from dacite import from_dict, Config as DConfig

from ..common import BaseService, SharedCoreResources
from .panel import PanelService
from .user.common import CommonUserService
from session import XUiSession
from custom_types import BandwidthInfo, BandwidthSnapshot, StateSnapshot
from errors import AppError, PanelUnavailableError

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

    def bandwidth(self,
                  username: str,
                  whitelist: bool = False
    ) -> BandwidthInfo:
        """Get bandwidth info about a user. Returns BandwidthInfo(upload, download, total). In bytes.

        The clients-first API keeps ONE shared ``client_traffics`` row per
        panel client — reading it per panel is the whole answer. Never sum
        it across inbounds; that double-counts. Cross-PANEL summation stays
        (a user is one client per panel).
        """
        self.user_svc.user(username)  # keeps the NotFoundError contract

        up_total = 0
        down_total = 0
        for panel in self._target_panels(whitelist):
            traffic = self.panel_svc.client_traffic(panel, username)
            if traffic is not None:
                up_total += traffic.up
                down_total += traffic.down

        return BandwidthInfo(up_total, down_total, up_total + down_total)

    def all_traffic(self, whitelist: bool = False) -> dict[str, BandwidthInfo]:
        """Batch path for pollers: one ``clients/list`` per target panel.

        Reduces every panel client into ``email -> BandwidthInfo`` summed
        across panels (never across a client's inbounds). Emails that are
        not known usernames are skipped. Raises ``PanelUnavailableError``
        when no target panel could be queried — the same rule
        ``get_online_status`` uses.
        """
        panels = self._target_panels(whitelist)
        if not panels:
            return {}
        totals: dict[str, list[int]] = {}
        queried = 0
        for panel in panels:
            try:
                clients = self.panel_svc.list_clients(panel)
            except AppError:
                self.log.error(
                    "client traffic listing failed for panel %s",
                    panel.name, exc_info=True,
                )
                continue
            queried += 1
            for client in clients:
                traffic = client.traffic
                if traffic is None or not self.db.user_exists(client.email):
                    continue
                acc = totals.setdefault(client.email, [0, 0])
                acc[0] += traffic.up
                acc[1] += traffic.down
        if queried == 0:
            raise PanelUnavailableError("No panel could be queried for client traffic")
        return {
            email: BandwidthInfo(up, down, up + down)
            for email, (up, down) in totals.items()
        }

    def get_bw_history(self, username: str, days: int = 30) -> list[BandwidthSnapshot]:
        """Return snapshots for a user, clamped to retention window."""
        cutoff = int(time.time()) - days * 86400
        return [BandwidthSnapshot(**row) for row in self.db.get_bandwidth_snapshots(username, cutoff)]

    def get_snapshots(self, days: int = 30) -> list[StateSnapshot]:
        """Return state snapshots, clamped to a retention window."""
        cutoff = int(time.time()) - days * 86400
        return [from_dict(StateSnapshot, row, config=DConfig(cast=[tuple]))
                for row in self.db.get_state_snapshots(cutoff)]
