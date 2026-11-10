from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from errors import AppError, NotFoundError, ValidationError
from util import generate_token

from ..common import BaseService
from ..coordination import panel_mutation, user_mutation

if TYPE_CHECKING:
    from ..common import SharedCoreResources
    from .password import PasswordService
    from .user import BusinessUserService, CommonUserService


@dataclass(frozen=True)
class PasswordReset:
    username: str
    uuid: str
    external: str
    password_hash: str | None
    expires: float


class AccountService(BaseService):
    """Account workflows sharing the website's persisted sessions."""

    def __init__(
        self,
        res: SharedCoreResources,
        *,
        password_svc: PasswordService,
        user_svc: CommonUserService,
        business_svc: BusinessUserService,
    ) -> None:
        super().__init__(res)
        self.password_svc = password_svc
        self.user_svc = user_svc
        self.business_svc = business_svc
        self._password_resets: dict[str, PasswordReset] = {}

    @user_mutation
    async def issue_password_reset(self, username: str) -> str:
        user = await self.user_svc.user(username)
        external = user["ext_username"]
        if not external:
            raise ValidationError("Account has no web login")
        now = time.monotonic()
        self._password_resets = {
            token: reset
            for token, reset in self._password_resets.items()
            if reset.expires > now and reset.username != username
        }
        token = secrets.token_urlsafe(56)
        self._password_resets[token] = PasswordReset(
            username, user["uuid"], external, user["ext_password_hash"], now + 3600
        )
        conf = self.cfg.view()
        prefix = "/".join(part for part in conf["uri"].split("/") if part)
        base = conf["domain"].rstrip("/") + (f"/{prefix}" if prefix else "")
        return f"{base}/reset?token={token}"

    async def password_reset(self, token: str | None) -> PasswordReset:
        reset = self._password_resets.get(token or "")
        if reset is not None:
            user = await self.db.get_user(reset.username)
            if (
                reset.expires > time.monotonic()
                and self._password_resets.get(token or "") is reset
                and user is not None
                and user["uuid"] == reset.uuid
                and user["ext_username"] == reset.external
                and user["ext_password_hash"] == reset.password_hash
            ):
                return reset
        self._password_resets.pop(token or "", None)
        raise NotFoundError("Not found")

    async def reset_password(self, token: str | None, password: str) -> None:
        reset = await self.password_reset(token)

        async def commit() -> None:
            await self.password_reset(token)
            self.password_svc.validate_new_password(password)
            hashed = await self.password_svc.hash(password)
            # Recheck after hashing: expiration and replacement can happen while awaiting.
            await self.password_reset(token)
            await self.db.reset_password(reset.username, hashed)
            self._password_resets.pop(token or "", None)
            await self.business_svc.audit_svc.audit_committed(
                name="user_password_reset", info={"username": reset.username}
            )

        await self.res.coordinator.run([reset.username], commit, name="core:password-reset")

    async def login(self, username: str, password: str) -> str:
        internal = await self.db.ext_to_user(username)
        if internal is None:
            raise AppError("Invalid credentials.", status=401)

        async def issue_session() -> str:
            # The external name may have moved while waiting for this user's lock.
            # Reject it before nested verification can resolve a different user.
            if await self.user_svc.get_external_username(internal) != username:
                raise AppError("Invalid credentials.", status=401)
            if await self.password_svc.validate_credentials(username, password) != internal:
                raise AppError("Invalid credentials.", status=401)
            token = generate_token("auth")
            await self.user_svc.set_auth_token(internal, token)
            return token

        return await self.res.coordinator.run([internal], issue_session, name="core:login")

    async def logout(self, username: str) -> None:
        await self.user_svc.set_auth_token(username, None)

    async def confirm_password(self, username: str, password: str | None) -> None:
        if not password:
            raise ValidationError("current_password required to change credentials")
        external = await self.user_svc.get_external_username(username)
        if not external:
            raise ValidationError("Account has no credentials set")
        if await self.password_svc.validate_credentials(external, password) != username:
            raise AppError("Invalid current password", status=401)

    @panel_mutation
    async def delete(self, username: str, password: str) -> None:
        self.require_panels()
        await self.confirm_password(username, password)
        await self.logout(username)
        await self.business_svc.delete_user(username=username)

    @user_mutation
    async def settings(
        self,
        username: str,
        *,
        displayname: str | None = None,
        fingerprint: str | None = None,
        ext_username: str | None = None,
        ext_password: str | None = None,
        current_password: str | None = None,
    ) -> None:
        # JSON callers can supply values outside the annotated service contract.
        for field, value in (
            ("name", displayname),
            ("fingerprint", fingerprint),
            ("username", ext_username),
            ("password", ext_password),
            ("current_password", current_password),
        ):
            if value is not None and not isinstance(value, str):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise ValidationError(f"Field '{field}' must be a string or null")
        if fingerprint and fingerprint not in self.cfg.view()["fingerprints"]:
            raise ValidationError("Unknown fingerprint")
        if displayname and len(displayname) > 16:
            raise ValidationError("displayname exceeds max. length of 16")
        if ext_username and len(ext_username) > 32:
            raise ValidationError("username exceeds max. length of 32")
        if ext_username is not None or ext_password is not None:
            await self.confirm_password(username, current_password)
        await self.business_svc.update_params(
            username=username,
            displayname=displayname,
            fingerprint=fingerprint,
            ext_username=ext_username,
            ext_password=ext_password,
        )
