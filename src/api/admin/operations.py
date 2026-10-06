from __future__ import annotations

from random import random

from flask import g

from api.admin._base import AdminApiMixin
from api.common import ResponseType, Route
from api.decorators import requires_admin_auth, requires_fields_strict
from errors import DatabaseError
from sysutil import SysUtil
from util import err, ok


class OperationsRoutes(AdminApiMixin):
    """Admin health, rollback, and novelty routes."""

    ROUTES = [
        Route('GET', '/api/health', 'health'),
        Route('GET', '/api/operations/status', 'operation_status'),
        Route('POST', '/api/operations/rollback/resolve', 'operation_rollback_resolve'),
        Route('GET', '/api/teapot', 'teapot'),
    ]

    @requires_admin_auth
    def health(self) -> ResponseType:
        status = SysUtil.health()
        memory = {
            "ram": round(status.memory.ram / 1024 / 1024, 2),
            "swap": round(status.memory.swap / 1024 / 1024, 2),
        }
        try:
            with self.sub.res.db.connection() as conn:
                conn.execute("SELECT 1")
        except DatabaseError:
            return err("Database unavailable", 503, obj={
                "db": False,
                "uptime": status.uptime,
                "memory": memory,
                "threads": status.threads,
            })
        snapshot_failure = self.bw.get_daily_snapshot_failure()
        rollback_failures = self.sub.business_code_svc.get_rollback_failures()
        return ok(obj={
            "db": True,
            "uptime": status.uptime,
            "memory": memory,
            "threads": status.threads,
            "degraded": snapshot_failure is not None or any(rollback_failures.values()),
            "daily_snapshot_failure": snapshot_failure,
            "rollback_failures": rollback_failures,
        })

    @requires_admin_auth
    def operation_status(self) -> ResponseType:
        return ok(obj={
            "daily_snapshot_failure": self.bw.get_daily_snapshot_failure(),
            "rollback_failures": self.sub.business_code_svc.get_rollback_failures(),
        })

    @requires_admin_auth
    @requires_fields_strict(('kind', str), ('user', str))
    def operation_rollback_resolve(self) -> ResponseType:
        content = g.json_obj
        kind_value: str = content.get('kind')
        username: str = content.get('user')
        if kind_value == 'uuid':
            self.sub.business_code_svc.clear_rollback_failure('uuid', username)
        elif kind_value == 'registration':
            self.sub.business_code_svc.clear_rollback_failure('registration', username)
        else:
            return err("kind must be 'uuid' or 'registration'")
        return ok("Resolved")

    def teapot(self) -> ResponseType:
        return err("I'm a teapot", 418, obj={"teapot": True if random() < 0.01 else False})  # is it really an error?
