from ...common import BaseService
from custom_types import UserRecord
from errors import NotFoundError

__all__ = ["CommonUserService"]

class CommonUserService(BaseService):
    def isuser(self, username: str) -> bool:
        """Know if a username exists."""
        return bool(username) and self.db.user_exists(username)

    def user(self, username: str) -> UserRecord:
        """Helper method"""
        user = self.db.get_user(username)
        if user is None:
            raise NotFoundError("Unknown username")
        return user

    def usertotoken(self, token: str) -> str | None:
        """Get a username from a token, None if doesnt exist."""
        if len(token) < 30:
            return None
        return self.db.token_to_user(token)

    def get_user_state(self, username: str) -> UserRecord:
        """Return a typed copy of the user's persisted state."""
        return self.user(username)

    def list_users(self) -> list[str]:
        """Return users in deterministic username order."""
        return self.db.list_users()

    def get_token(self, username: str) -> str:
        return str(self.user(username)["token"])


    def auth_token_to_user(self, auth_token: str) -> str | None:
        """Get a username from a web auth token, None if it does not exist."""
        if len(auth_token) != 100:
            return None
        return self.db.auth_token_to_user(auth_token)


    def set_auth_token(self, username: str, auth_token: str | None) -> None:
        self.user(username)
        self.db.set_auth_token(username, auth_token)

    def get_fingerprint(self, username: str) -> str:
        return str(self.user(username)["fingerprint"])

    def get_external_username(self, username: str) -> str | None:
        return self.db.user_to_ext(username)

    def external_username_exists(self, ext_username: str) -> bool:
        return self.db.ext_to_user(ext_username) is not None

