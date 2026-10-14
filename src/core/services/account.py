from __future__ import annotations

from typing import TYPE_CHECKING

from errors import AppError, ValidationError
from util import generate_token

if TYPE_CHECKING:
    from core import Subscription


class AccountService:
    """Account workflows sharing the website's persisted sessions."""

    def __init__(self, sub: Subscription) -> None:
        self.sub = sub

    async def login(self, username: str, password: str) -> str:
        internal = await self.sub.password_svc.validate_credentials(username, password)
        if internal is None:
            raise AppError("Invalid credentials.", status=401)
        token = generate_token("auth")
        await self.sub.user_svc.set_auth_token(internal, token)
        return token

    async def logout(self, username: str) -> None:
        await self.sub.user_svc.set_auth_token(username, None)

    async def confirm_password(self, username: str, password: str | None) -> None:
        if not password:
            raise ValidationError("current_password required to change credentials")
        external = await self.sub.user_svc.get_external_username(username)
        if not external:
            raise ValidationError("Account has no credentials set")
        if await self.sub.password_svc.validate_credentials(external, password) != username:
            raise AppError("Invalid current password", status=401)

    async def delete(self, username: str, password: str) -> None:
        await self.confirm_password(username, password)
        await self.logout(username)
        await self.sub.business_svc.delete_user(username=username, perma=True)

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
        if fingerprint and fingerprint not in self.sub.res.cfg.view()["fingerprints"]:
            raise ValidationError("Unknown fingerprint")
        if displayname and len(displayname) > 16:
            raise ValidationError("displayname exceeds max. length of 16")
        if ext_username and len(ext_username) > 32:
            raise ValidationError("username exceeds max. length of 32")
        if ext_username or ext_password:
            await self.confirm_password(username, current_password)
        await self.sub.business_svc.update_params(
            username=username,
            displayname=displayname,
            fingerprint=fingerprint,
            ext_username=ext_username,
            ext_password=ext_password,
        )
