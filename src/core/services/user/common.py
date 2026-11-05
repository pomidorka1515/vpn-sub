from __future__ import annotations

import re
from typing import TYPE_CHECKING

from errors import ConflictError, NotFoundError

from ...common import BaseService
from ...coordination import user_mutation

if TYPE_CHECKING:
    from custom_types import UserRecord


def valid_subscription_token(token: str) -> bool:
    # Tokens are interpolated into subscription URLs across the UI and bots.
    return re.fullmatch(r"[A-Za-z0-9._~-]{30,}", token) is not None


class CommonUserService(BaseService):
    async def user(self, username: str) -> UserRecord:
        user = await self.db.get_user(username)
        if user is None:
            raise NotFoundError("Unknown username")
        return user

    async def isuser(self, username: str) -> bool:
        return bool(username) and await self.db.user_exists(username)

    async def usertotoken(self, token: str) -> str | None:
        return await self.db.token_to_user(token) if valid_subscription_token(token) else None

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
        await self.require_uuid_recovered(username)
        await self.user(username)
        await self.db.set_auth_token(username, auth_token)

    async def require_uuid_recovered(self, username: str) -> None:
        if await self.db.get_metadata(f"uuid_rollback_failed:{username}") is not None:
            raise ConflictError("User requires UUID recovery before further mutations")

    async def get_fingerprint(self, username: str) -> str:
        return (await self.user(username))["fingerprint"]

    async def get_external_username(self, username: str) -> str | None:
        return await self.db.user_to_ext(username)

    async def external_username_exists(self, ext_username: str) -> bool:
        return await self.db.ext_to_user(ext_username) is not None
