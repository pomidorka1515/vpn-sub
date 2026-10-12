from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal, cast

from quart import g

from api.admin._base import AsyncAdminApiMixin
from api.common import Route
from api.config_patch import apply_config_patch, config_etag
from api.decorators import requires_admin_auth, requires_fields_strict
from errors import SchemaValidationError, ValidationError
from util import err, ok

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from api.common import ResponseType
    from config import JsonValue
    from custom_types import JsonifyValue


class AsyncConfigRoutes(AsyncAdminApiMixin):
    """Admin config read and patch routes."""

    ROUTES: ClassVar[tuple[Route, ...]] = (
        Route("GET", "/api/config/get", "config_get"),
        Route("POST", "/api/config/set", "config_set"),
    )

    @requires_admin_auth
    async def config_get(self) -> ResponseType:
        data = self.cfg.view()
        response, code = ok(obj=cast("JsonifyValue", data))
        response.headers["ETag"] = f'"{config_etag(cast("dict[str, JsonValue]", data))}"'
        response.headers["Cache-Control"] = "no-store"
        return response, code

    @requires_admin_auth
    @requires_fields_strict()
    async def config_set(self) -> ResponseType:
        body = cast("dict[str, JsonValue]", g.json_obj)
        base = body.get("base")
        values = body.get("values")
        if not isinstance(base, str) or not isinstance(values, dict):
            return err("base must be a string and values must be an object")

        if any(not isinstance(key, str) for key in cast("dict[object, object]", values)):
            return err("values must be an object")
        base = base.strip().removeprefix("W/").strip().strip('"')

        conflict: str | None = None
        statuses: list[Literal["updated", "unchanged"]] = ["unchanged"]
        changed: list[str] = []
        restart: list[str] = []

        def patch(tx: MutableMapping[str, JsonValue]) -> None:
            nonlocal conflict, changed, restart
            current_hash = config_etag(dict(tx))
            if current_hash != base:
                conflict = current_hash
                return
            status, changed, restart = apply_config_patch(tx, values)
            statuses[0] = status

        try:
            await self.cfg.mutate(patch)
        except ValidationError as exc:
            return err(exc.message)
        except SchemaValidationError as exc:
            # Message includes the path. Do not log it: jsonschema echoes the value.
            return err(exc.message)

        if conflict is not None:
            return err("Config changed since it was loaded", 409, {"base": conflict})

        status = statuses[0]
        if status == "updated":
            # The file is already replaced. A backup failure must not turn a
            # committed edit into a 500, and must not skip the audit.
            try:
                await self.cfg.backup_now()
            except Exception:
                self.log.exception("config backup failed after commit")
            await self.sub.audit_svc.audit(
                name="config_update",
                info={"keys": changed},
            )
        new_hash = config_etag(cast("dict[str, JsonValue]", self.cfg.view()))
        return ok(
            "Updated" if status == "updated" else "Unchanged",
            obj={"base": new_hash, "restart": restart},
        )
