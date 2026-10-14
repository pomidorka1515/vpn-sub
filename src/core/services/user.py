from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from dataclasses import asdict
from typing import TYPE_CHECKING, cast
from urllib.parse import quote

from config import JsonValue
from custom_types import (
    ClientPayload,
    NewUserInfo,
    ResetUserObject,
    UserInfo,
    UserInfoBandwidth,
    UserInfoBandwidthTotal,
)
from errors import (
    AppError,
    ConflictError,
    DuplicateError,
    NotFoundError,
    PanelRejectedError,
    ValidationError,
)
from util import generate_token, isusername, isuuid, sanitize

from ..common import BaseService
from ..coordination import user_mutation
from .client_payload import already_exists, client_payload_from

if TYPE_CHECKING:
    from collections.abc import Mapping

    from custom_types import PanelClient, UserFields, UserRecord
    from session.client import XUiSession

    from ..common import SharedCoreResources
    from .audit import AuditService
    from .bandwidth import BandwidthService
    from .panel import PanelService, WorkClass
    from .password import PasswordService


class CommonUserService(BaseService):
    async def user(self, username: str) -> UserRecord:
        user = await self.db.get_user(username)
        if user is None:
            raise NotFoundError("Unknown username")
        return user

    async def isuser(self, username: str) -> bool:
        return bool(username) and await self.db.user_exists(username)

    async def usertotoken(self, token: str) -> str | None:
        return await self.db.token_to_user(token) if len(token) >= 30 else None

    async def get_user_state(self, username: str) -> UserRecord:
        return await self.user(username)

    async def list_users(self) -> list[str]:
        return await self.db.list_users()

    async def list_user_states(self) -> list[UserRecord]:
        return await self.db.list_user_records()

    async def get_token(self, username: str) -> str:
        return (await self.user(username))["token"]

    async def auth_token_to_user(self, auth_token: str) -> str | None:
        return await self.db.auth_token_to_user(auth_token) if len(auth_token) == 100 else None

    @user_mutation
    async def set_auth_token(self, username: str, auth_token: str | None) -> None:
        await self.user(username)
        await self.db.set_auth_token(username, auth_token)

    async def get_fingerprint(self, username: str) -> str:
        return (await self.user(username))["fingerprint"]

    async def get_external_username(self, username: str) -> str | None:
        return await self.db.user_to_ext(username)

    async def external_username_exists(self, ext_username: str) -> bool:
        return await self.db.ext_to_user(ext_username) is not None


class BusinessUserService(BaseService):
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
            _=random.choice(conf.get("funny_strings", [])),
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
                monthly=user["bw_used"],
                wl_monthly=user["wl_used"],
                limit=user["bw_limit_gb"],
                wl_limit=user["wl_limit_gb"],
            ),
        )

    @user_mutation
    async def add_users(
        self,
        username: str,
        _called_internally: bool = False,
        *,
        work: WorkClass = "foreground",
    ) -> None:
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
        if not _called_internally:
            await self.audit_svc.audit(name="user_refresh", info={"username": username})
        await self._drop_cache()

    @user_mutation
    async def rollback_panel_clients(
        self,
        username: str,
        *,
        expected_uuid: str | None = None,
        work: WorkClass = "foreground",
    ) -> None:
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

    @user_mutation
    async def delete_user(
        self,
        username: str,
        perma: bool = False,
        *,
        work: WorkClass = "foreground",
    ) -> None:
        # Retrying a failed delete is safe; other pending workflows reserve
        # their identity until their own compensation has completed.
        if (
            await self.db.registration_pending(username)
            or await self.db.get_metadata(f"user_create_pending:{username}") is not None
        ):
            raise ConflictError("User requires registration recovery")
        user = await self.user_svc.user(username)
        key = f"user_delete_pending:{username}"
        await self.db.set_metadata(key, json.dumps({"uuid": user["uuid"], "perma": perma}))
        await self.rollback_panel_clients(username, expected_uuid=user["uuid"], work=work)
        if perma:
            await self.db.delete_user(username)
            await self.db.delete_metadata(f"user_sync_pending:{username}")
            await self.db.delete_metadata(f"user_reset_pending:{username}")
        await self.db.delete_metadata(key)
        await self.audit_svc.audit(name="user_delete", info={"username": username, "perma": perma})
        await self._drop_cache()

    async def _set_enabled(
        self,
        panel: XUiSession,
        username: str,
        enabled: bool,
        clients: Mapping[str, PanelClient],
    ) -> None:
        if clients.get(username) is None:
            return
        action = "bulkEnable" if enabled else "bulkDisable"
        await self._post(
            panel,
            f"panel/api/clients/{action}",
            {"emails": [username]},
            "user update",
        )

    @user_mutation
    async def update_user(
        self,
        username: str,
        enable: bool | None = None,
        timee: bool | None = None,
        wl_enable: bool | None = None,
        *,
        work: WorkClass = "foreground",
    ) -> None:
        await self.require_recovered(username)
        await self.user_svc.user(username)
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
        )
        await self._sync_enabled(
            username,
            main=enable is not None,
            whitelist=wl_enable is not None,
            work=work,
        )
        await self.audit_svc.audit(
            name="user_update",
            info={"username": username, **cast(dict[str, JsonValue], dict(fields))},
        )
        await self._drop_cache()

    async def _sync_enabled(
        self,
        username: str,
        *,
        main: bool = True,
        whitelist: bool = True,
        work: WorkClass = "foreground",
    ) -> None:
        user = await self.user_svc.user(username)
        panels = [
            panel for panel in self.panels if (whitelist if panel == self.whitelist_panel else main)
        ]
        known = await self.panel_svc.client_maps(panels, work=work)

        async def write(panel: XUiSession) -> None:
            enabled = bool(user["enabled_wl"] if panel == self.whitelist_panel else user["enabled"])
            await self._set_enabled(panel, username, enabled, known[panel.name])

        await self.panel_svc.map_panels(panels, write, work=work)
        await self.db.delete_metadata(f"user_sync_pending:{username}")

    @user_mutation
    async def apply_quota(self, username: str, *, now: int | None = None) -> None:
        """Watcher decisions use current limits/expiry under shared ownership."""
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
            work="background",
        )

    @user_mutation
    async def add_new_user(
        self,
        username: str,
        displayname: str,
        ext_username: str | None = None,
        ext_password: str | None = None,
        token: str | None = None,
        userid: str | None = None,
        fingerprint: str | None = None,
        limit: int = 0,
        wl_limit: int = 5,
        timee: int = 0,
    ) -> NewUserInfo:
        await self.require_recovered(username)
        if not isusername(username):
            raise ValidationError("Invalid username")
        if len(displayname) > 16:
            raise ValidationError("Displayname too long")
        if ext_username is not None and len(ext_username) > 32:
            raise ValidationError("Ext Username too long")
        if userid is not None and not isuuid(userid):
            raise ValidationError("Invalid UUID")
        conf = self.cfg.view()
        fingerprint = random.choice(conf["fingerprints"]) if fingerprint is None else fingerprint
        if fingerprint not in conf["fingerprints"]:
            raise ValidationError("Invalid fingerprint")
        if timee > 2**31:
            raise ValidationError("Invalid timestamp")
        token = generate_token("sub") if token is None else token
        userid = str(uuid.uuid4()) if userid is None else userid
        displayname = sanitize(displayname, "display")
        hashed = await self.password_svc.hash(ext_password) if ext_password is not None else None
        external = sanitize(ext_username, "external") if ext_username and hashed else None
        try:
            await self.db.create_user(
                username=username,
                uuid=userid,
                token=token,
                fingerprint=fingerprint,
                displayname=displayname,
                expires_at=timee,
                bw_limit_gb=limit,
                wl_limit_gb=wl_limit,
                ext_username=external,
                ext_password_hash=hashed,
                pending_sync=True,
            )
        except DuplicateError as exc:
            raise ConflictError("Username or external username exists") from exc
        # Marker precedes writes and remains if panel cleanup cannot finish.
        try:
            await self.add_users(username, _called_internally=True)
        except Exception:
            try:
                await self.rollback_panel_clients(username, expected_uuid=userid)
                await self.db.rollback_created_user(username, userid)
            except Exception:
                self.log.critical(
                    "new user rollback failed for %s; marker retained",
                    username,
                    exc_info=True,
                )
            raise
        await self.db.delete_metadata(f"user_create_pending:{username}")
        await self.audit_svc.audit(
            name="user_add",
            info={"username": username, "ext_username": external},
        )
        return NewUserInfo(
            username=username,
            token=token,
            uuid=userid,
            fingerprint=fingerprint,
            displayname=displayname,
        )

    @user_mutation
    async def update_params(
        self,
        username: str,
        displayname: str | None = None,
        ext_username: str | None = None,
        ext_password: str | None = None,
        token: str | None = None,
        fingerprint: str | None = None,
        limit: int | None = None,
        wl_limit: int | None = None,
        timee: int | None = None,
    ) -> None:
        await self.require_recovered(username)
        user = await self.user_svc.user(username)
        fields: UserFields = {}
        if displayname is not None:
            if len(displayname) > 16:
                raise ValidationError("Displayname too long")
            fields["displayname"] = sanitize(displayname, "display")
        if ext_username is not None:
            if len(ext_username) > 32:
                raise ValidationError("Username too long")
            fields["ext_username"] = sanitize(ext_username, "external")
        if fingerprint is not None:
            if fingerprint not in self.cfg.view()["fingerprints"]:
                raise ValidationError("Invalid fingerprint")
            fields["fingerprint"] = fingerprint
        if timee is not None:
            if timee > 2**31:
                raise ValidationError("Invalid time")
            fields["expiry_time"] = timee
        if token is not None:
            fields["token"] = token
        if limit is not None:
            fields["bw_limit"] = limit
        if wl_limit is not None:
            fields["wl_bw_limit"] = wl_limit
        if ext_password is not None:
            fields["ext_password"] = await self.password_svc.hash(ext_password)
        if (
            fields.get("ext_password") is not None
            and user["ext_username"] is None
            and ext_username is None
        ):
            raise ValidationError("External username required")
        try:
            await self.db.update_user(username, **fields)
        except DuplicateError as exc:
            raise ConflictError("Ext username exists") from exc
        audit_fields = cast(dict[str, JsonValue], dict(fields))
        await self.audit_svc.audit(
            name="user_update_params",
            info={
                "username": username,
                **{
                    key: value
                    for key, value in audit_fields.items()
                    if key not in {"token", "ext_password"}
                },
            },
        )
        await self._drop_cache()

    @user_mutation
    async def update_uuid(self, username: str, uid: str, *, _resetting: bool = False) -> None:
        await self.require_recovered(username, allow_reset=_resetting)
        if not isuuid(uid):
            raise ValidationError("Invalid UUID")
        await self.user_svc.user(username)
        key = f"uuid_rollback_failed:{username}"
        # Persist before writing: crash or forced runtime cancellation retains
        # the same manual-repair marker used by the existing service.
        await self.db.set_metadata(key, f"{int(time.time())}:UUID update in progress")

        async def write(panel: XUiSession) -> None:
            current = await self.panel_svc.get_client(panel, username)
            if current is not None:
                payload = client_payload_from(current)
                payload.id = uid
                await self._post(
                    panel,
                    f"panel/api/clients/update/{quote(username, safe='')}",
                    asdict(payload),
                    "UUID update",
                )

        outcomes = await self.panel_svc.outcomes(self.panels, write)
        for outcome in outcomes:
            if outcome.error is not None:
                reason = (
                    outcome.error.message
                    if isinstance(outcome.error, AppError)
                    else type(outcome.error).__name__
                )
                await self.db.set_metadata(key, f"{int(time.time())}:{outcome.panel}: {reason}")
                raise outcome.error
        try:
            await self.db.update_user(username, uuid=uid)
        except DuplicateError as exc:
            await self.db.set_metadata(key, f"{int(time.time())}:db duplicate uuid")
            raise ConflictError("UUID exists") from exc
        await self.db.delete_metadata(key)
        await self.audit_svc.audit(
            name="user_update_uuid",
            info={"username": username, "uuid": uid},
        )
        await self._drop_cache()

    @user_mutation
    async def reset_user(self, username: str) -> ResetUserObject:
        await self.require_recovered(username)
        uid, token = str(uuid.uuid4()), generate_token("sub")
        await self.db.set_metadata(
            f"user_reset_pending:{username}",
            json.dumps({"uuid": uid, "token": token}),
        )
        await self.update_uuid(username, uid, _resetting=True)
        await self.db.finalize_reset(username, uid, token)
        await self.audit_svc.audit(
            name="user_reset",
            info={"username": username, "uuid": uid, "token": "redacted"},
        )
        return ResetUserObject(uuid=uid, token=token)

    @user_mutation
    async def reconcile_user(self, username: str) -> None:
        await self.require_recovered(username)
        if await self.db.get_metadata(f"uuid_rollback_failed:{username}") is not None:
            raise ConflictError("User requires pending operation recovery before reconcile")
        await self.db.set_metadata(f"user_sync_pending:{username}", "enabled")
        await self.add_users(username, _called_internally=True, work="background")
        await self._sync_enabled(username, work="background")

    async def reconcile(self) -> list[str]:
        users = await self.user_svc.list_users()
        pending = iter(users)
        failures: set[str] = set()

        async def worker() -> None:
            # Taking the next user has no await, so workers share the iterator safely.
            for username in pending:
                try:
                    await self.reconcile_user(username)
                except Exception:
                    failures.add(username)
                    self.log.exception("user reconcile failed for %s", username)

        async with asyncio.TaskGroup() as group:
            for index in range(min(4, len(users))):
                group.create_task(worker(), name=f"core:reconcile-worker:{index}")
        return [username for username in users if username in failures]

    async def recover_pending(self) -> None:
        for prefix in (
            "user_delete_pending:",
            "user_create_pending:",
            "user_reset_pending:",
            "user_sync_pending:",
        ):
            for key, value in (await self.db.list_metadata(prefix)).items():
                username = key.removeprefix(prefix)

                async def repair(
                    username: str = username,
                    key: str = key,
                    value: str = value,
                    prefix: str = prefix,
                ) -> None:
                    user = await self.db.get_user(username)
                    if prefix == "user_create_pending:":
                        await self.rollback_panel_clients(username, expected_uuid=value)
                        await self.db.rollback_created_user(username, value)
                    elif prefix == "user_delete_pending:":
                        details = json.loads(value)
                        expected = str(details["uuid"])
                        if user is not None and user["uuid"] != expected:
                            raise ConflictError("Delete recovery identity changed")
                        await self.rollback_panel_clients(username, expected_uuid=expected)
                        if details["perma"]:
                            await self.db.delete_user(username)
                            await self.db.delete_metadata(f"user_sync_pending:{username}")
                            await self.db.delete_metadata(f"user_reset_pending:{username}")
                    elif prefix == "user_reset_pending:":
                        details = json.loads(value)
                        await self.db.finalize_reset(
                            username,
                            str(details["uuid"]),
                            str(details["token"]),
                        )
                    elif user is not None:
                        await self.reconcile_user(username)
                    await self.db.delete_metadata(key)

                try:
                    await self.res.coordinator.run([username], repair, name="core:recover-user")
                except Exception:
                    self.log.critical(
                        "user recovery failed for %s; marker retained",
                        username,
                        exc_info=True,
                    )
