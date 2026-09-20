import time
from dacite import from_dict, Config as DConfig

from core.common import SharedCoreResources

from ..common import BaseService
from .panel import PanelService
from .user import UserService
from session import XUiSession
from custom_types import BandwidthInfo, BandwidthSnapshot, StateSnapshot

__all__ = ["BandwidthService"]

class BandwidthService(BaseService):
    def __init__(
        self, 
        res: SharedCoreResources, 
        panel_service: PanelService,
        user_service: UserService
    ) -> None:
        super().__init__(res)
        self.panel_svc: PanelService = panel_service
        self.user_svc: UserService = user_service
     
    def bandwidth(self, 
                  username: str,
                  whitelist: bool = False
    ) -> BandwidthInfo:
        """Get bandwidth info about a user. Returns BandwidthInfo(upload, download, total). In bytes."""
        userid = str(self.user_svc.user(username)["uuid"])

        if whitelist:
            # No whitelist panel configured -> there is nothing to report.
            # NOTE: don't fall through to the loop below with a bare `[None]` list,
            # `getinbounds()` dereferences attributes on `panel` immediately and
            # would raise an AttributeError on a None panel.
            if not self.whitelist_panel:
                return BandwidthInfo(0, 0, 0)
            panels: list[XUiSession] = [self.whitelist_panel]
        else:
            panels = [p for p in self.panels if p != self.whitelist_panel]

        if not panels:
            return BandwidthInfo(0, 0, 0)
        up_total = 0
        down_total = 0
        for panel in panels:
            inbounds = self.panel_svc.getinbounds(panel)
            for i in inbounds:
                for v in (i.clientStats or []):
                    if v.uuid == userid:
                        up_total += v.up
                        down_total += v.down
                        break
        
        return BandwidthInfo(up_total, down_total, up_total + down_total)
    
    def get_bw_history(self, username: str, days: int = 30) -> list[BandwidthSnapshot]:
        """Return snapshots for a user, clamped to retention window."""
        cutoff = int(time.time()) - days * 86400
        return [BandwidthSnapshot(**row) for row in self.db.get_bandwidth_snapshots(username, cutoff)]

    def get_snapshots(self, days: int = 30) -> list[StateSnapshot]:
        """Return state snapshots, clamped to a retention window."""
        cutoff = int(time.time()) - days * 86400
        return [from_dict(StateSnapshot, row, config=DConfig(cast=[tuple]))
                for row in self.db.get_state_snapshots(cutoff)]
        