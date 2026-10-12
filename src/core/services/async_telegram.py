from __future__ import annotations

from typing import TYPE_CHECKING, Literal, overload

from errors import NotFoundError

from ..async_common import AsyncBaseService

if TYPE_CHECKING:
    from custom_types import ApplyBonusCodeObject, UserInfo

    from ..async_common import AsyncCoreResources
    from .async_code import AsyncCodeService
    from .async_user import AsyncBusinessUserService


class AsyncTelegramService(AsyncBaseService):
    def __init__(
        self,
        res: AsyncCoreResources,
        *,
        user_svc: AsyncBusinessUserService,
        code_svc: AsyncCodeService,
    ) -> None:
        super().__init__(res)
        self.user_svc = user_svc
        self.code_svc = code_svc

    async def get_telegram_language(self, tgid: int | str) -> str:
        return await self.db.get_telegram_language(tgid)

    async def has_telegram_language(self, tgid: int | str) -> bool:
        return await self.db.has_telegram_language(tgid)

    async def set_telegram_language(self, tgid: int | str, language: str) -> None:
        await self.db.set_telegram_language(tgid, language)

    async def get_info_telegram(self, tgid: int) -> UserInfo:
        username = await self.db.tgid_to_user(tgid)
        if username is None:
            raise NotFoundError("Unknown Telegram user")
        return await self.user_svc.get_info(username)

    async def is_registered(self, tgid: int) -> bool:
        return await self.db.tgid_to_user(tgid) is not None

    async def set_telegram_user(self, tgid: int | str, username: str | None) -> None:
        # SQL mapping updates are atomic. Serialize the Telegram identity too,
        # so clearing a mapping cannot erase a concurrent relink.
        async def update() -> None:
            if username is None:
                existing = await self.db.tgid_to_user(tgid)
                if existing is not None:
                    await self.db.set_telegram(existing, None)
            else:
                await self.db.set_telegram(username, str(tgid))

        keys = [f"telegram:{tgid}"] + ([username] if username is not None else [])
        await self.res.coordinator.run(keys, update, name="core:telegram-link")

    async def bonus_code(self, value: int | str, code: str) -> ApplyBonusCodeObject:
        username = await self.get_username_telegram(value)
        if username is None:
            raise NotFoundError("Unknown Telegram user")
        return await self.code_svc.apply_bonus_code(username=username, code=code)

    @overload
    async def get_username_telegram(
        self, tgid: int | str, reverse: Literal[False] = False,
    ) -> str | None: ...

    @overload
    async def get_username_telegram(
        self, tgid: int | str, reverse: Literal[True],
    ) -> int | None: ...

    async def get_username_telegram(
        self, tgid: int | str, reverse: bool = False,
    ) -> int | str | None:
        if not reverse:
            return await self.db.tgid_to_user(tgid)
        mapped = await self.db.user_to_tgid(str(tgid))
        return int(mapped) if mapped is not None else None
