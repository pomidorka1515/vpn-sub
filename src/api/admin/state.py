from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, ClassVar, Literal

from quart import Response, g

from api.admin._base import AsyncAdminApiMixin
from api.common import Route
from api.decorators import requires_admin_auth, requires_fields_strict
from custom_types import PollingPanelInfo
from sysutil import SysUtil
from util import err, ok

if TYPE_CHECKING:
    from api.common import ResponseType
    from custom_types import JsonifyValue

    from .live import LiveState


class AsyncStateRoutes(AsyncAdminApiMixin):
    """Admin host, panel, snapshot, and leaderboard routes."""

    live_state: LiveState

    ROUTES: ClassVar[tuple[Route, ...]] = (
        Route("POST", "/api/leaderboard", "leaderboard"),
        Route("POST", "/api/state/snapshots", "snapshots"),
        Route("GET", "/api/state/all", "full_info"),
        Route("GET", "/api/state/system", "system_status"),
        Route("GET", "/api/state/polling", "polling_status"),
    )

    @requires_admin_auth
    async def system_status(self) -> ResponseType:
        return ok(obj=asdict(await self.sub.res.offloads.metrics.run(SysUtil.full_info)))

    @requires_admin_auth
    async def polling_status(self) -> ResponseType:
        """Stream shared dynamic state; unavailable panels are null."""
        response = Response(
            self.live_state.stream(),
            content_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        response.timeout = None
        return response

    async def _polling_sample(self) -> JsonifyValue:
        panels_data: dict[str, dict[str, JsonifyValue] | None] = {}
        for panel, res in zip(self.sub.panels, await self.sub.panel_svc.statuses(), strict=True):
            if res is None:
                panels_data[panel.name] = None
                continue
            obj = res.obj
            panels_data[panel.name] = asdict(
                PollingPanelInfo(
                    app_stats=obj.appStats,
                    cpu=obj.cpu,
                    disk=obj.disk,
                    loads=obj.loads,
                    mem=obj.mem,
                    netIO=obj.netIO,
                    netTraffic=obj.netTraffic,
                    swap=obj.swap,
                    tcpCount=obj.tcpCount,
                    udpCount=obj.udpCount,
                    uptime=obj.uptime,
                ),
            )
        return {
            "host": asdict(await self.sub.res.offloads.metrics.run(SysUtil.polling_info)),
            "panels": panels_data,
        }

    @requires_admin_auth
    async def full_info(self) -> ResponseType:
        panels_data: dict[str, JsonifyValue] = {}
        for panel, res in zip(self.sub.panels, await self.sub.panel_svc.statuses(), strict=True):
            if res is None:
                panels_data[panel.name] = {"status": "unknown"}
            else:
                panels_data[panel.name] = asdict(res)

        obj: dict[str, JsonifyValue] = {
            "host": asdict(await self.sub.res.offloads.metrics.run(SysUtil.full_info)),
            "panels": panels_data,
        }
        return ok(obj=obj)

    @requires_admin_auth
    @requires_fields_strict(("cutoff", int))
    async def snapshots(self) -> ResponseType:
        content = g.json_obj

        cutoff: int = content.get("cutoff")

        if cutoff < 0:
            return err("cutoff param must be higher than 0", 400)

        obj = await self.sub.bandwidth_svc.get_snapshots(cutoff)
        return ok(obj=[asdict(s) for s in obj])

    @requires_admin_auth
    @requires_fields_strict(("type", str), ("cutoff", int), ("displaynames", bool), ("flip", bool))
    async def leaderboard(self) -> ResponseType:
        content = g.json_obj

        category: Literal["total", "monthly", "wl_monthly"] = content.get("type")
        top_n: int = content.get("cutoff")
        use_displaynames: bool = content.get("displaynames")
        flip: bool = content.get("flip")

        if category not in {"total", "monthly", "wl_monthly"}:
            return err(msg="category field must be either 'total', 'monthly' or 'wl_monthly'")

        data = await self.sub.leaderboard_svc.leaderboard(
            category=category,
            top_n=top_n,
            use_displaynames=use_displaynames,
            flip=flip,
        )
        return ok(
            obj=[
                {"place": i, "username": user, "amount": amount}
                for i, (user, amount) in enumerate(data.items(), start=1)
            ],
        )
