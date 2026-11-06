from __future__ import annotations

import time
from dataclasses import asdict
from typing import TYPE_CHECKING, cast
from urllib.parse import quote

from config import JsonValue
from custom_types import ClientPayload
from errors import PanelRejectedError

from ...coordination import panel_mutation
from ..client_payload import already_exists
from .identity import UserIdentityService

if TYPE_CHECKING:
    from collections.abc import Mapping

    from custom_types import PanelClient, UserFields
    from session.client import XUiSession

    from ..panel import WorkClass


class UserPanelService(UserIdentityService):
    @panel_mutation
    async def add_users(
        self,
        username: str,
        _called_internally: bool = False,
        *,
        work: WorkClass = "foreground",
    ) -> None:
        self.require_panels()
        await self.user_svc.require_uuid_recovered(username)
        if not _called_internally:
            await self.require_recovered(username)
        user = await self.user_svc.user(username)
        # Re-read under ownership rather than trusting a bulk snapshot captured
        # before another mutation acquired this user's lock.
        known = await self.panel_svc.client_maps(self.panels, work=work)
        if await self.db.get_metadata(f"user_sync_pending:{username}") is None:
            await self.db.set_metadata(f"user_sync_pending:{username}", "reconcile")

        async def sync(panel: XUiSession) -> None:
            inbound_ids = [
                row.id for row in await self.panel_svc.getinbounds(panel) if row.protocol == "vless"
            ]
            if not inbound_ids:
                return
            existing = known[panel.name].get(username)
            if existing is None:
                payload = ClientPayload(
                    email=username,
                    id=user["uuid"],
                    flow="xtls-rprx-vision",
                    limitIp=0,
                    totalGB=0,
                    expiryTime=0,
                    enable=bool(
                        user["enabled_wl"] if panel == self.whitelist_panel else user["enabled"],
                    ),
                    tgId=0,
                    subId="",
                    comment="",
                    reset=0,
                )
                try:
                    await self._post(
                        panel,
                        "panel/api/clients/add",
                        {"client": asdict(payload), "inboundIds": inbound_ids},
                        "user add",
                    )
                    return
                except PanelRejectedError as exc:
                    if not already_exists(exc):
                        raise
                    existing = await self.panel_svc.get_client(panel, username)
                    if existing is None or existing.uuid != user["uuid"]:
                        raise
            if existing.uuid != user["uuid"]:
                raise PanelRejectedError(
                    f"Panel {panel.name} email {username!r} is owned by a different UUID",
                )
            missing = [value for value in inbound_ids if value not in existing.inboundIds]
            if missing:
                await self._post(
                    panel,
                    f"panel/api/clients/{quote(username, safe='')}/attach",
                    {"inboundIds": missing},
                    "user attach",
                )

        await self.panel_svc.map_panels(self.panels, sync, work=work)
        if await self.db.get_metadata(f"user_sync_pending:{username}") == "reconcile":
            await self.db.delete_metadata(f"user_sync_pending:{username}")
        await self._drop_cache()
        if not _called_internally:
            await self.audit_svc.audit_committed(name="user_refresh", info={"username": username})

    @panel_mutation
    async def rollback_panel_clients(
        self,
        username: str,
        *,
        expected_uuid: str | None = None,
        work: WorkClass = "foreground",
    ) -> None:
        self.require_panels()
        await self.user_svc.require_uuid_recovered(username)

        async def delete(panel: XUiSession) -> None:
            # Live reads make repeated compensation safe; unavailable is never
            # confused with absent, and foreign shared-panel clients survive.
            client = await self.panel_svc.get_client(panel, username)
            if client is None:
                return
            if expected_uuid is not None and client.uuid != expected_uuid:
                raise PanelRejectedError(f"Panel {panel.name} rollback refused a foreign UUID")
            await self._post(
                panel,
                f"panel/api/clients/del/{quote(username, safe='')}",
                {},
                "user delete",
            )

        await self.panel_svc.map_panels(self.panels, delete, work=work)

    async def _set_enabled(
        self,
        panel: XUiSession,
        username: str,
        enabled: bool,
        clients: Mapping[str, PanelClient],
        expected_uuid: str,
    ) -> None:
        client = clients.get(username)
        if client is None:
            return
        if client.uuid != expected_uuid:
            raise PanelRejectedError(
                f"Panel {panel.name} email {username!r} is owned by a different UUID",
            )
        action = "bulkEnable" if enabled else "bulkDisable"
        await self._post(
            panel,
            f"panel/api/clients/{action}",
            {"emails": [username]},
            "user update",
        )

    @panel_mutation
    async def update_user(
        self,
        username: str,
        enable: bool | None = None,
        timee: bool | None = None,
        wl_enable: bool | None = None,
        *,
        quota: bool = False,
        work: WorkClass = "foreground",
        notification_intent: str | None = None,
    ) -> None:
        """Set explicit intent unless this is a quota-derived status change."""
        self.require_panels()
        await self.require_recovered(username)
        await self.user_svc.user(username)
        pending_sync = await self.db.get_metadata(f"user_sync_pending:{username}") is not None
        fields: UserFields = {}
        if enable is not None:
            fields["status"] = enable
            if timee is not None:
                fields["status_time"] = timee
        if wl_enable is not None:
            fields["status_wl"] = wl_enable
        await self.db.set_enabled_pending(
            username,
            enabled=enable,
            enabled_time=timee,
            enabled_wl=wl_enable,
            quota=quota,
            notification_intent=notification_intent,
        )
        if pending_sync:
            # The previous intent may cover other panels or unfinished client
            # creation/attachment. Repair it before clearing the shared marker.
            await self.reconcile_user(username)
        else:
            await self._sync_enabled(
                username,
                main=enable is not None,
                whitelist=wl_enable is not None,
                work=work,
            )
        await self._drop_cache()
        await self.audit_svc.audit_committed(
            name="user_update",
            info={"username": username, **cast(dict[str, JsonValue], dict(fields))},
        )

    async def _sync_enabled(
        self,
        username: str,
        *,
        main: bool = True,
        whitelist: bool = True,
        work: WorkClass = "foreground",
    ) -> None:
        self.require_panels()
        user = await self.user_svc.user(username)
        panels = [
            panel for panel in self.panels if (whitelist if panel == self.whitelist_panel else main)
        ]
        known = await self.panel_svc.client_maps(panels, work=work)

        async def write(panel: XUiSession) -> None:
            enabled = bool(user["enabled_wl"] if panel == self.whitelist_panel else user["enabled"])
            await self._set_enabled(panel, username, enabled, known[panel.name], user["uuid"])

        await self.panel_svc.map_panels(panels, write, work=work)
        await self.db.delete_metadata(f"user_sync_pending:{username}")

    @panel_mutation
    async def apply_quota(self, username: str, *, now: int | None = None) -> None:
        """Watcher decisions use current limits/expiry under shared ownership."""
        self.require_panels()
        user = await self.user_svc.user(username)
        current = int(time.time()) if now is None else now
        time_ok = user["expires_at"] == 0 or user["expires_at"] >= current
        main_ok = user["bw_limit_gb"] == 0 or user["bw_used"] < user["bw_limit_gb"] * 10**9
        wl_ok = user["wl_limit_gb"] == 0 or user["wl_used"] < user["wl_limit_gb"] * 10**9
        await self.update_user(
            username,
            enable=time_ok and main_ok,
            timee=time_ok,
            wl_enable=wl_ok,
            quota=True,
            work="background",
        )

    @panel_mutation
    async def reconcile_user(self, username: str) -> None:
        self.require_panels()
        await self._recover_reset(username)
        await self.require_recovered(username)
        await self.db.set_metadata(f"user_sync_pending:{username}", "enabled")
        await self.add_users(username, _called_internally=True, work="background")
        await self._sync_enabled(username, work="background")
