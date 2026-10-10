from __future__ import annotations

from collections.abc import MutableMapping
from typing import ClassVar, Literal, cast

from flask import g

from api.admin._base import AdminApiMixin
from api.common import ResponseType, Route
from api.config_patch import apply_config_patch, config_etag
from api.decorators import requires_admin_auth, requires_fields_strict
from config import JsonValue
from custom_types import JsonifyValue
from errors import ConfigError, SchemaValidationError, ValidationError
from util import err, ok


class ConfigRoutes(AdminApiMixin):
    """Admin config read and patch routes."""

    ROUTES: ClassVar[tuple[Route, ...]] = (
        Route('GET', '/api/config/get', 'config_get'),
        Route('POST', '/api/config/set', 'config_set'),
    )

    @requires_admin_auth
    def config_get(self) -> ResponseType:
        data = self.cfg.copy()
        response, code = ok(obj=cast(JsonifyValue, data))
        response.headers["ETag"] = f'"{config_etag(data)}"'
        response.headers["Cache-Control"] = "no-store"
        return response, code

    @requires_admin_auth
    @requires_fields_strict()
    def config_set(self) -> ResponseType:
        body = cast(dict[str, JsonValue], g.json_obj)
        base = body.get("base")
        values = body.get("values")
        if not isinstance(base, str) or not isinstance(values, dict):
            return err("base must be a string and values must be an object")

        if any(not isinstance(key, str) for key in cast(dict[object, object], values)):
            return err("values must be an object")
        base = base.strip().removeprefix("W/").strip().strip('"')

        conflict: str | None = None
        status: Literal["updated", "unchanged"] = "unchanged"
        changed: list[str] = []
        restart: list[str] = []
        written: dict[str, JsonValue] | None = None
        try:
            with self.cfg.edit() as tx:
                current = tx.copy()
                current_hash = config_etag(current)
                if current_hash != base:
                    conflict = current_hash
                if conflict is None:
                    status, changed, restart = apply_config_patch(
                        cast(MutableMapping[str, JsonValue], tx),
                        values,
                    )
                    if status == "updated":
                        # Fail before commit. __exit__ validates again, then replaces.
                        self.cfg.validate_document(tx.copy())
                        written = tx.copy()
        except ValidationError as exc:
            return err(exc.message)
        except SchemaValidationError as exc:
            # Message includes the path. Do not log it: jsonschema echoes the value.
            return err(exc.message)

        if conflict is not None:
            return err("Config changed since it was loaded", 409, {"base": conflict})

        if status == "updated":
            if written is None:
                raise ConfigError("Config update was not committed")
            # The file is already replaced. A backup failure must not turn a
            # committed edit into a 500, and must not skip the audit.
            try:
                self.cfg.backup_data(written)
            except Exception:
                self.log.exception("config backup failed after commit")
            self.sub.audit_svc.audit(
                name="config_update",
                info={"keys": changed},
            )
        new_hash = config_etag(self.cfg.copy())
        return ok(
            "Updated" if status == "updated" else "Unchanged",
            obj={"base": new_hash, "restart": restart},
        )
