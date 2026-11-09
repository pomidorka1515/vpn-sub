from __future__ import annotations

import asyncio
import json
import random
import uuid
from dataclasses import asdict
from typing import TYPE_CHECKING

from custom_types import NewUserInfo
from errors import ConflictError, DuplicateError, ValidationError
from util import generate_token, isusername, isuuid, sanitize

from ...coordination import panel_mutation
from .common import valid_subscription_token
from .panels import UserPanelService

if TYPE_CHECKING:
    from ..panel import WorkClass


class BusinessUserService(UserPanelService):
    @panel_mutation
    async def delete_user(
        self,
        username: str,
        *,
        work: WorkClass = "foreground",
    ) -> None:
        self.require_panels()
        # Retrying a failed delete is safe; other pending workflows reserve
        # their identity until their own compensation has completed.
        await self.user_svc.require_uuid_recovered(username)
        if (
            await self.db.registration_pending(username)
            or await self.db.get_metadata(f"user_create_pending:{username}") is not None
        ):
            raise ConflictError("User requires registration recovery")
        user = await self.user_svc.user(username)
        key = f"user_delete_pending:{username}"
        await self.db.set_metadata(key, json.dumps({"uuid": user["uuid"]}))
        await self.rollback_panel_clients(username, expected_uuid=user["uuid"], work=work)
        await self.db.delete_user(username)
        await self.db.delete_metadata(f"user_sync_pending:{username}")
        await self.db.delete_metadata(f"user_reset_pending:{username}")
        await self.db.delete_metadata(key)
        await self._drop_cache()
        await self.audit_svc.audit_committed(name="user_delete", info={"username": username})

    @panel_mutation
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
        self.require_panels()
        await self.require_recovered(username)
        if not isusername(username):
            raise ValidationError("Invalid username")
        if len(displayname) > 16:
            raise ValidationError("Displayname too long")
        if ext_username is not None and len(ext_username) > 32:
            raise ValidationError("Ext Username too long")
        if ext_password is not None and not ext_username:
            raise ValidationError("External username required")
        if ext_password is not None:
            self.password_svc.validate_new_password(ext_password)
        if userid is not None and not isuuid(userid):
            raise ValidationError("Invalid UUID")
        conf = self.cfg.view()
        fingerprint = random.choice(conf["fingerprints"]) if fingerprint is None else fingerprint
        if fingerprint not in conf["fingerprints"]:
            raise ValidationError("Invalid fingerprint")
        if type(timee) is not int or not 0 <= timee <= 2**63 - 1:
            raise ValidationError("Invalid timestamp")
        token = generate_token("sub") if token is None else token
        if not valid_subscription_token(token):
            raise ValidationError("Subscription token must contain at least 30 URL-safe characters")
        userid = str(uuid.uuid4()) if userid is None else userid
        displayname = sanitize(displayname, "display")
        external = (
            sanitize(ext_username, "external")
            if ext_username and ext_password is not None
            else None
        )
        hashed = await self.password_svc.hash(ext_password) if ext_password is not None else None
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
        result = NewUserInfo(
            username=username,
            token=token,
            uuid=userid,
            fingerprint=fingerprint,
            displayname=displayname,
        )
        await self.audit_svc.audit_committed(
            name="user_add",
            info={"username": username, "ext_username": external},
            result=asdict(result),
        )
        return result

    async def reconcile(self) -> list[str]:
        self.require_panels()
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
        if not self.panels:
            return
        for prefix in (
            "user_delete_pending:",
            "user_create_pending:",
            "user_reset_pending:",
            "uuid_rollback_failed:",
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
                    # The snapshot may predate a live mutation finishing while
                    # recovery waited for ownership. Never replay stale intent.
                    if await self.db.get_metadata(key) != value:
                        return
                    user = await self.db.get_user(username)
                    if prefix == "user_create_pending:":
                        await self.rollback_panel_clients(username, expected_uuid=value)
                        await self.db.rollback_created_user(username, value)
                    elif prefix == "user_delete_pending:":
                        await self.user_svc.require_uuid_recovered(username)
                        details = json.loads(value)
                        expected = str(details["uuid"])
                        if user is not None and user["uuid"] != expected:
                            raise ConflictError("Delete recovery identity changed")
                        await self.rollback_panel_clients(username, expected_uuid=expected)
                        # Preserve old panel-only deletion intent during upgrades.
                        # New markers omit the retired flag and always delete SQLite.
                        if details.get("perma", True):
                            await self.db.delete_user(username)
                            await self.db.delete_metadata(f"user_sync_pending:{username}")
                            await self.db.delete_metadata(f"user_reset_pending:{username}")
                    elif prefix == "user_reset_pending:":
                        await self._recover_reset(username)
                    elif prefix == "uuid_rollback_failed:":
                        # A standalone marker has no old/new UUID intent. Keep
                        # it (and log the failure) until an operator repairs the
                        # panels and explicitly clears it. Never infer ownership.
                        await self._recover_reset(username)
                        await self.user_svc.require_uuid_recovered(username)
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
