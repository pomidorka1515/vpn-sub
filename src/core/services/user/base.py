from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from custom_types import BandwidthInfo, UserInfo, UserInfoBandwidth, UserInfoBandwidthTotal
from errors import ConflictError, PanelRejectedError

from ...common import BaseService

if TYPE_CHECKING:
    from collections.abc import Mapping

    from config import JsonValue
    from session.client import XUiSession

    from ...common import SharedCoreResources
    from ..audit import AuditService
    from ..bandwidth import BandwidthService
    from ..panel import PanelService
    from ..password import PasswordService
    from .common import CommonUserService


class BusinessUserBase(BaseService):
    def __init__(
        self,
        res: SharedCoreResources,
        *,
        user_svc: CommonUserService,
        panel_svc: PanelService,
        password_svc: PasswordService,
        audit_svc: AuditService,
        bandwidth_svc: BandwidthService,
    ) -> None:
        super().__init__(res)
        self.user_svc = user_svc
        self.panel_svc = panel_svc
        self.password_svc = password_svc
        self.audit_svc = audit_svc
        self.bandwidth_svc = bandwidth_svc

    async def require_recovered(self, username: str, *, allow_reset: bool = False) -> None:
        """A retained identity is reserved for recovery, not a new workflow."""
        # Only update_uuid's validated durable reset replay may bypass this.
        # Standalone UUID failures require manual repair and explicit clearance.
        if not allow_reset:
            await self.user_svc.require_uuid_recovered(username)
        if await self.db.registration_pending(username):
            raise ConflictError("User has a pending registration")
        for prefix in (
            "registration_rollback_failed:",
            "user_create_pending:",
            "user_delete_pending:",
        ):
            if await self.db.get_metadata(f"{prefix}{username}") is not None:
                raise ConflictError("User requires pending operation recovery")
        if (
            not allow_reset
            and await self.db.get_metadata(f"user_reset_pending:{username}") is not None
        ):
            raise ConflictError("User requires reset recovery")

    async def _post(
        self,
        panel: XUiSession,
        route: str,
        body: Mapping[str, JsonValue],
        what: str,
    ) -> None:
        # Even an unavailable response may follow an accepted external write.
        # Invalidate on every attempted write, and preserve the original error.
        try:
            response = await panel.post(route, json=body, headers={"Accept": "application/json"})
            data = self.panel_svc.response_data(panel, response, what)
            if not data.get("success"):
                raise PanelRejectedError(f"Panel {panel.name} {what} rejected: {data.get('msg')}")
        except Exception:
            try:
                await self.panel_svc.invalidate_clients(panel)
            except Exception:
                self.log.exception(
                    "cache invalidation failed after rejected write on %s",
                    panel.name,
                )
            raise
        else:
            await self.panel_svc.invalidate_clients(panel)

    async def _drop_cache(self) -> None:
        for panel in self.panels:
            await self.panel_svc.invalidate_inbounds(panel)

    async def get_info(self, username: str, pretty: bool = False) -> UserInfo:
        """Return usage in bytes, or decimal MB when pretty; limits remain in GB."""
        self.require_panels()
        conf = self.cfg.view()
        user = await self.user_svc.user(username)
        bw, wl, online = await asyncio.gather(
            self.bandwidth_svc.bandwidth(username),
            self.bandwidth_svc.bandwidth(username, whitelist=True),
            self.panel_svc.is_online(username),
        )
        if pretty:
            bw, wl = bw.format_all_mb(), wl.format_all_mb()
        return UserInfo(
            token=user["token"],
            link=f"{conf['domain']}/{conf['uri'].strip('/')}?token={user['token']}",
            displayname=user["displayname"],
            uuid=user["uuid"],
            fingerprint=user["fingerprint"],
            enabled=bool(user["enabled"]),
            wl_enabled=bool(user["enabled_wl"]),
            time=user["expires_at"],
            online=online,
            bandwidth=UserInfoBandwidth(
                total=UserInfoBandwidthTotal(
                    upload=bw.upload,
                    download=bw.download,
                    total=bw.total,
                ),
                wl_total=UserInfoBandwidthTotal(
                    upload=wl.upload,
                    download=wl.download,
                    total=wl.total,
                ),
                monthly=(
                    BandwidthInfo.format_bytes_mb(user["bw_used"]) if pretty else user["bw_used"]
                ),
                wl_monthly=(
                    BandwidthInfo.format_bytes_mb(user["wl_used"]) if pretty else user["wl_used"]
                ),
                limit=user["bw_limit_gb"],
                wl_limit=user["wl_limit_gb"],
            ),
        )
