from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, ClassVar

from quart import request

from api.admin._base import AsyncAdminApiMixin
from api.common import Route
from api.decorators import requires_admin_auth
from util import err, ok

if TYPE_CHECKING:
    from api.common import ResponseType
    from custom_types import JsonifyValue


class AsyncPanelRoutes(AsyncAdminApiMixin):
    """Admin panel status routes."""

    ROUTES: ClassVar[tuple[Route, ...]] = (Route("GET", "/api/panel/status", "panel_status"),)

    @requires_admin_auth
    async def panel_status(self) -> ResponseType:
        query = request.args.get("name", None)
        if query is None:
            result: dict[str, dict[str, JsonifyValue] | None] = {}
            for panel, status in zip(
                self.sub.panels, await self.sub.panel_svc.statuses(), strict=True,
            ):
                _ = status
                if _ is None:
                    self.log.error(f"getstatus: {panel.name} returned None")
                else:
                    _ = asdict(_)
                result[panel.name] = _ if _ is not None else {"status": "unknown"}
            return ok(obj=result)

        for panel in self.sub.panels:
            if panel.name == query:
                res = await self.sub.panel_svc.getstatus(panel)
                if res is None:
                    return err("getstatus() returned None; panel may be down")
                return ok(obj=asdict(res))

        return err("panel not found", 404)
