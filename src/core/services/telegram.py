
from ..common import BaseService
from custom_types import UserInfo
from errors import NotFoundError

__all__ = ["TelegramService"]

class TelegramService(BaseService):
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
        return self.get_info(username, True)
    
    def is_registered(self, tgid: int) -> bool:
        """Check if a telegram user is already registered."""
        return self.db.tgid_to_user(tgid) is not None
