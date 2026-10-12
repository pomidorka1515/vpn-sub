from __future__ import annotations

import hashlib

from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from util import compare

from ..async_common import AsyncBaseService


class AsyncPasswordService(AsyncBaseService):
    async def hash(self, value: str) -> str:
        return await self.res.offloads.passwords.run(self.res.password_hasher.hash, value)

    def legacy_hash(self, value: str) -> str:
        return hashlib.sha256((self.res.legacy_salt + value).encode()).hexdigest()

    async def validate_credentials(self, ext_username: str, password: str) -> str | None:
        username = await self.db.ext_to_user(ext_username)
        if username is None:
            return None

        async def verify() -> str | None:
            # Re-read the row while coordinated with password/login changes.
            user = await self.db.get_user(username)
            if user is None or user["ext_username"] != ext_username:
                return None
            stored = user["ext_password_hash"]
            if stored is None:
                return None
            if stored.startswith("$argon2id$"):
                try:
                    await self.res.offloads.passwords.run(
                        self.res.password_hasher.verify, stored, password
                    )
                except VerifyMismatchError:
                    return None
                except InvalidHashError, VerificationError:
                    self.log.exception(
                        "Corrupt or tampered argon2 password hash for %s", ext_username
                    )
                    return None
            else:
                if not compare(stored, self.legacy_hash(password)):
                    return None
                await self.db.set_user(username, ext_password=await self.hash(password))
            return username

        return await self.res.coordinator.run([username], verify, name="core:verify-password")
