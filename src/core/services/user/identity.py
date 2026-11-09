from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict
from typing import TYPE_CHECKING, Unpack, cast
from urllib.parse import quote

from config import JsonValue
from custom_types import ResetUserObject
from errors import (
    AppError,
    CommittedAuditError,
    ConflictError,
    DuplicateError,
    NotFoundError,
    PanelRejectedError,
    ValidationError,
)
from util import generate_token, isuuid, sanitize

from ...coordination import panel_mutation, user_mutation
from ..client_payload import client_payload_from
from .base import BusinessUserBase
from .common import valid_subscription_token

if TYPE_CHECKING:
    from custom_types import UserFields, UserUpdateFields
    from session.client import XUiSession


class UserIdentityService(BusinessUserBase):
    async def _recover_reset(self, username: str) -> None:
        value = await self.db.get_metadata(f"user_reset_pending:{username}")
        if value is None:
            return
        details = json.loads(value)
        uid = str(details["uuid"])
        # Replay even if SQLite already has the UUID: a previous attempt may
        # have stopped between individual panel writes.
        uuid_audit_error: CommittedAuditError | None = None
        try:
            await self.update_uuid(username, uid, _resetting=True)
        except CommittedAuditError as exc:
            uuid_audit_error = exc
        await self.db.finalize_reset(username, uid, str(details["token"]))
        if uuid_audit_error is not None:
            raise CommittedAuditError(
                result=asdict(ResetUserObject(uuid=uid, token=str(details["token"]))),
            ) from uuid_audit_error

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
        fields = await self._prepare_params(
            displayname=displayname,
            ext_username=ext_username,
            ext_password=ext_password,
            token=token,
            fingerprint=fingerprint,
            limit=limit,
            wl_limit=wl_limit,
            timee=timee,
        )
        if ext_password is not None and user["ext_username"] is None and ext_username is None:
            raise ValidationError("External username required")
        await self._commit_params(username, fields)
        audit_fields = cast(dict[str, JsonValue], dict(fields))
        await self.audit_svc.audit_committed(
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

    async def _prepare_params(
        self,
        *,
        displayname: str | None = None,
        ext_username: str | None = None,
        ext_password: str | None = None,
        token: str | None = None,
        fingerprint: str | None = None,
        limit: int | None = None,
        wl_limit: int | None = None,
        timee: int | None = None,
    ) -> UserFields:
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
        for name, value, maximum in (
            ("time", timee, 2**63 - 1),
            ("limit", limit, (2**63 - 1) // 10**9),
            ("wl_limit", wl_limit, (2**63 - 1) // 10**9),
        ):
            if value is not None and (type(value) is not int or not 0 <= value <= maximum):
                raise ValidationError(f"{name} must be an integer in range 0 to {maximum}")
        if timee is not None:
            fields["expiry_time"] = timee
        if token is not None:
            if not valid_subscription_token(token):
                raise ValidationError(
                    "Subscription token must contain at least 30 URL-safe characters"
                )
            fields["token"] = token
        if limit is not None:
            fields["bw_limit"] = limit
        if wl_limit is not None:
            fields["wl_bw_limit"] = wl_limit
        if ext_password is not None:
            self.password_svc.validate_new_password(ext_password)
            fields["ext_password"] = await self.password_svc.hash(ext_password)
        return fields

    async def _commit_params(self, username: str, fields: UserFields) -> None:
        try:
            await self.db.update_user(username, **fields)
        except DuplicateError as exc:
            raise ConflictError("Ext username exists") from exc
        await self._drop_cache()

    async def edit_user(self, username: str, **changes: Unpack[UserUpdateFields]) -> list[str]:
        async def update() -> list[str]:
            await self.require_recovered(username)
            user = await self.user_svc.user(username)
            if "new_username" in changes and user["ext_username"] is None:
                raise NotFoundError("Unknown external username")
            fields = await self._prepare_params(
                ext_username=changes.get("new_username"),
                displayname=changes.get("displayname"),
                fingerprint=changes.get("fingerprint"),
                timee=changes.get("timee"),
                limit=changes.get("limit"),
                wl_limit=changes.get("wl_limit"),
            )
            for key, column, value in (
                ("used", "bw_used", changes.get("used")),
                ("wl_used", "wl_bw_used", changes.get("wl_used")),
            ):
                if value is not None:
                    if type(value) is not int or not 0 <= value <= 2**63 - 1:
                        raise ValidationError(f"{key} must be an integer in range 0 to {2**63 - 1}")
                    if column == "bw_used":
                        fields["bw_used"] = value
                    else:
                        fields["wl_bw_used"] = value
            updated = [
                public
                for key, public in (
                    ("new_username", "username"),
                    ("displayname", "displayname"),
                    ("fingerprint", "fingerprint"),
                    ("timee", "time"),
                    ("limit", "limit"),
                    ("used", "used"),
                    ("wl_limit", "wl_limit"),
                    ("wl_used", "wl_used"),
                )
                if key in changes
            ]
            await self._commit_params(username, fields)
            await self.audit_svc.audit_committed(
                name="user_edit",
                info={
                    "username": username,
                    "user": username,
                    **cast(dict[str, JsonValue], dict(changes)),
                },
                result={"updated": cast(list[JsonValue], updated)},
            )
            return updated

        return await self.res.coordinator.run([username], update, name="core:user-edit")

    @panel_mutation
    async def update_uuid(self, username: str, uid: str, *, _resetting: bool = False) -> None:
        self.require_panels()
        await self.require_recovered(username, allow_reset=_resetting)
        if not isuuid(uid):
            raise ValidationError("Invalid UUID")
        user = await self.user_svc.user(username)
        key = f"uuid_rollback_failed:{username}"
        expected_uuids = {user["uuid"]}
        if _resetting:
            pending = await self.db.get_metadata(f"user_reset_pending:{username}")
            if pending is None:
                raise ConflictError("User has no pending reset")
            details = json.loads(pending)
            old_uuid = str(details.get("old_uuid", user["uuid"]))
            if details["uuid"] != uid or user["uuid"] not in {old_uuid, uid}:
                raise ConflictError("Reset recovery identity changed")
            # Only a durable reset intent proves ownership of both identities.
            expected_uuids.update((old_uuid, uid))
        # The claim and recovery marker must commit before any external write.
        try:
            await self.db.reserve_uuid(
                username, user["uuid"], uid, f"{int(time.time())}:UUID update in progress"
            )
        except DuplicateError as exc:
            raise ConflictError("UUID exists") from exc

        async def write(panel: XUiSession) -> None:
            current = await self.panel_svc.get_client(panel, username)
            if current is not None:
                if current.uuid not in expected_uuids:
                    raise PanelRejectedError(
                        f"Panel {panel.name} email {username!r} is owned by a different UUID",
                    )
                if current.uuid == uid:
                    return
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
        await self.db.finalize_uuid(username, user["uuid"], uid)
        await self._drop_cache()
        await self.audit_svc.audit_committed(
            name="user_update_uuid",
            info={"username": username, "uuid": uid},
        )

    @panel_mutation
    async def reset_user(self, username: str) -> ResetUserObject:
        self.require_panels()
        await self._recover_reset(username)
        await self.require_recovered(username)
        user = await self.user_svc.user(username)
        uid, token = str(uuid.uuid4()), generate_token("sub")
        try:
            await self.db.reserve_uuid(
                username,
                user["uuid"],
                uid,
                f"{int(time.time())}:UUID update in progress",
                reset=json.dumps({"uuid": uid, "token": token, "old_uuid": user["uuid"]}),
            )
        except DuplicateError as exc:
            raise ConflictError("UUID exists") from exc
        uuid_audit_error: CommittedAuditError | None = None
        try:
            await self.update_uuid(username, uid, _resetting=True)
        except CommittedAuditError as exc:
            uuid_audit_error = exc
        await self.db.finalize_reset(username, uid, token)
        result = ResetUserObject(uuid=uid, token=token)
        await self.audit_svc.audit_committed(
            name="user_reset",
            info={"username": username, "uuid": uid, "token": "redacted"},
            result=asdict(result),
        )
        if uuid_audit_error is not None:
            raise CommittedAuditError(result=asdict(result)) from uuid_audit_error
        return result
