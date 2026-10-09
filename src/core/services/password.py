import hashlib
from argon2.exceptions import VerificationError, InvalidHashError, VerifyMismatchError

from util import compare
from ..common import BaseService
from tracer import Op

__all__ = ["PasswordService"]

class PasswordService(BaseService):
    def hash(self, s: str) -> str:
        return self.password_hasher.hash(s)
    
    def legacy_hash(self, s: str) -> str:
        return hashlib.sha256((self.legacy_salt + s).encode()).hexdigest()

    def validate_credentials(self, ext_username: str, password: str) -> str | None:
        stored = self.db.ext_password(ext_username)
        username = self.db.ext_to_user(ext_username)
        self.trace(
            Op.password.validate_credentials, "start",
            ext_username=ext_username,
            found=stored is not None and username is not None,
        )
        if stored is None or username is None:
            self.trace(
                Op.password.validate_credentials, "absent",
                ext_username=ext_username, username=username,
            )
            return None
        if stored.startswith("$argon2id$"):
            try:
                self.password_hasher.verify(stored, password)
            except VerifyMismatchError:
                self.trace(
                    Op.password.validate_credentials, "mismatch",
                    ext_username=ext_username, username=username, scheme="argon2id",
                )
                return None
            except (InvalidHashError, VerificationError):
                self.log.error(
                    "Corrupt or tampered argon2 password hash for %s",
                    ext_username,
                    exc_info=True,
                )
                self.trace(
                    Op.password.validate_credentials, "corrupt",
                    ext_username=ext_username, username=username, scheme="argon2id",
                )
                return None
            self.trace(
                Op.password.validate_credentials, "ok",
                ext_username=ext_username, username=username, scheme="argon2id",
                migrated=False,
            )
        else:
            if compare(stored, self.legacy_hash(password)):
                self.db.set_user(username, ext_password=self.hash(password))
                self.trace(
                    Op.password.validate_credentials, "migrated",
                    ext_username=ext_username, username=username, scheme="legacy",
                    migrated=True,
                )
                return username
            self.trace(
                Op.password.validate_credentials, "mismatch",
                ext_username=ext_username, username=username, scheme="legacy",
            )
            return None
        return username
