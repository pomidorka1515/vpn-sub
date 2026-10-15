from __future__ import annotations

from typing import TYPE_CHECKING

from errors import AppError, ValidationError
from util import generate_token

from ..common import BaseService

if TYPE_CHECKING:
    from ..common import SharedCoreResources
    from .password import PasswordService
    from .user import BusinessUserService, CommonUserService


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

    async def login(self, username: str, password: str) -> str:
        internal = await self.password_svc.validate_credentials(username, password)
        if internal is None:
            raise AppError("Invalid credentials.", status=401)
        token = generate_token("auth")
        await self.user_svc.set_auth_token(internal, token)
        return token

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

    async def delete(self, username: str, password: str) -> None:
        self.require_panels()
        await self.confirm_password(username, password)
        await self.logout(username)
        await self.business_svc.delete_user(username=username, perma=True)

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
        if fingerprint and fingerprint not in self.cfg.view()["fingerprints"]:
            raise ValidationError("Unknown fingerprint")
        if displayname and len(displayname) > 16:
            raise ValidationError("displayname exceeds max. length of 16")
        if ext_username and len(ext_username) > 32:
            raise ValidationError("username exceeds max. length of 32")
        if ext_username or ext_password:
            await self.confirm_password(username, current_password)
        await self.business_svc.update_params(
            username=username,
            displayname=displayname,
            fingerprint=fingerprint,
            ext_username=ext_username,
            ext_password=ext_password,
        )
