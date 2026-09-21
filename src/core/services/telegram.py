from typing import overload, Literal

from ..common import BaseService, SharedCoreResources
from .user.business import BusinessUserService
from .code import CodeService
from custom_types import UserInfo, ApplyBonusCodeObject
from errors import NotFoundError

__all__ = ["TelegramService"]

class TelegramService(BaseService):
    def __init__(
        self,
        res: SharedCoreResources,
        *,
        user_svc: BusinessUserService,
        code_svc: CodeService
    ) -> None:
        super().__init__(res)
        self.user_svc: BusinessUserService = user_svc
        self.code_svc: CodeService = code_svc
    
    def get_telegram_language(self, tgid: int | str) -> str:
        return self.db.get_telegram_language(tgid)

    def has_telegram_language(self, tgid: int | str) -> bool:
        return self.db.has_telegram_language(tgid)

    def set_telegram_language(self, tgid: int | str, language: str) -> None:
        self.db.set_telegram_language(tgid, language)
    
    def get_info_telegram(self, tgid: int) -> UserInfo:
        """Returns all user info by telegram ID. Raises NotFoundError if absent."""
        username = self.db.tgid_to_user(tgid)
        if username is None:
            raise NotFoundError("Unknown Telegram user")
        return self.user_svc.get_info(username, True)
    
    def is_registered(self, tgid: int) -> bool:
        """Check if a telegram user is already registered."""
        return self.db.tgid_to_user(tgid) is not None

    def set_telegram_user(self, tgid: int | str, username: str | None) -> None:
        if username is None:
            existing = self.db.tgid_to_user(tgid)
            if existing is not None:
                self.db.set_telegram(existing, None)
            return
        self.db.set_telegram(username, str(tgid))
    
    def bonus_code(self, value: int | str, code: str) -> ApplyBonusCodeObject:
        """Apply a bonus code for a Telegram user."""
        username = self.get_username_telegram(value)
        if not isinstance(username, str) or not username:
            raise NotFoundError("Unknown Telegram user")
        return self.code_svc.apply_bonus_code(username=username, code=code)
    
    @overload
    def get_username_telegram(self, tgid: int | str, reverse: Literal[False] = False) -> str | None: ...

    @overload
    def get_username_telegram(self, tgid: int | str, reverse: Literal[True]) -> int | None: ...

    def get_username_telegram(self, tgid: int | str, reverse: bool = False) -> int | str | None:
        """Get the internal username for a tgid.
        Parameter reverse: if True, get tg id from username. Otherwise default behaviour."""

        if not reverse:
            return self.db.tgid_to_user(tgid)
        mapped = self.db.user_to_tgid(str(tgid))
        return int(mapped) if mapped is not None else None
