from __future__ import annotations

from typing import ClassVar

from flask import request

from api.admin._base import AdminApiMixin
from api.common import ResponseType, Route
from api.decorators import requires_admin_auth
from util import err, ok


class LogsRoutes(AdminApiMixin):
    """Admin audit-log routes."""

    ROUTES: ClassVar[tuple[Route, ...]] = (
        Route('GET', '/api/logs/audit', 'audit'),
    )

    @requires_admin_auth
    def audit(self) -> ResponseType:
        n = request.args.get('n', 50, type=int)

        if n < 0:
            return err("'n' arg must be a positive integer, or 0 for the whole file")

        if n == 0:
            return ok(obj=self.audit_cfg.read_all())

        result = list(self.audit_cfg.tail(n))
        return ok(obj=result)
