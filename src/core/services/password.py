import hashlib
from argon2.exceptions import VerificationError, InvalidHashError, VerifyMismatchError

from util import compare
from ..common import BaseService

__all__ = ["PasswordService"]

class PasswordService(BaseService):
    def hash(self, s: str) -> str:
        return self.password_hasher.hash(s)
    
    def legacy_hash(self, s: str) -> str:
        return hashlib.sha256((self.legacy_salt + s).encode()).hexdigest()

    def validate_credentials(self, ext_username: str, password: str) -> str | None:
        stored = self.db.ext_password(ext_username)
        username = self.db.ext_to_user(ext_username)
        if stored is None or username is None:
            return None
        if stored.startswith("$argon2id$"):
            try:
                self.password_hasher.verify(stored, password)
            except VerifyMismatchError:
                return None
            except (InvalidHashError, VerificationError):
                self.log.error(
                    "Corrupt or tampered argon2 password hash for %s",
                    ext_username,
                    exc_info=True,
                )
                return None
        else:
            if compare(stored, self.legacy_hash(password)):
                self.db.set_user(username, ext_password=self.hash(password))
                return username
            return None
        return username
