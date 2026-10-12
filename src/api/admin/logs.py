from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from quart import request

from api.admin._base import AsyncAdminApiMixin
from api.common import Route
from api.decorators import requires_admin_auth
from util import err, ok

if TYPE_CHECKING:
    from api.common import ResponseType


class AsyncLogsRoutes(AsyncAdminApiMixin):
    """Admin audit-log routes."""

    ROUTES: ClassVar[tuple[Route, ...]] = (Route("GET", "/api/logs/audit", "audit"),)

    @requires_admin_auth
    async def audit(self) -> ResponseType:
        n = request.args.get("n", 50, type=int)

        if n < 0:
            return err("'n' arg must be a positive integer, or 0 for the whole file")

        if n == 0:
            return ok(obj=await self.audit_cfg.read_all())

        result = await self.audit_cfg.tail(n)
        return ok(obj=result)
