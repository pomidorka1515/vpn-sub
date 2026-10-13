from __future__ import annotations

import time
from typing import TYPE_CHECKING, Unpack, cast

from custom_types import UserRecord
from errors import DatabaseError, DuplicateError

from .common import ConnectionMixin
from .telegram import set_telegram_mapping

if TYPE_CHECKING:
    from custom_types import UserFields


class UsersMixin(ConnectionMixin):
    async def get_user(self, username: str) -> UserRecord | None:
        async with self.connection() as conn:
            row = await conn.row("SELECT * FROM users WHERE username = ?", (username,))
            return cast(UserRecord, dict(row)) if row is not None else None

    async def list_users(self) -> list[str]:
        return [user["username"] for user in await self.list_user_records()]

    async def list_user_records(self) -> list[UserRecord]:
        async with self.connection() as conn:
            return [
                cast(UserRecord, dict(row))
                for row in await conn.rows("SELECT * FROM users ORDER BY username")
            ]

    async def all_users(self) -> list[str]:
        return await self.list_users()

    async def user_exists(self, username: str) -> bool:
        async with self.connection() as conn:
            return await conn.row("SELECT 1 FROM users WHERE username = ?", (username,)) is not None

    async def usernames(self) -> set[str]:
        async with self.connection() as conn:
            return {str(row[0]) for row in await conn.rows("SELECT username FROM users")}

    async def username_exts(self) -> dict[str, str | None]:
        async with self.connection() as conn:
            return {
                str(row[0]): None if row[1] is None else str(row[1])
                for row in await conn.rows("SELECT username, ext_username FROM users")
            }

    async def create_user(
        self,
        *,
        username: str,
        uuid: str,
        token: str,
        fingerprint: str,
        displayname: str,
        expires_at: int = 0,
        bw_limit_gb: int = 0,
        wl_limit_gb: int = 0,
        ext_username: str | None = None,
        ext_password_hash: str | None = None,
        enabled: bool = True,
        enabled_time: bool = True,
        enabled_wl: bool = True,
        pending_sync: bool = False,
    ) -> None:
        try:
            async with self.transaction(immediate=True) as conn:
                await conn.execute(
                    """INSERT INTO users
                    (username, uuid, token, fingerprint, displayname, enabled,
                     enabled_time, enabled_wl, expires_at, bw_limit_gb, wl_limit_gb,
                     ext_username, ext_password_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        username,
                        uuid,
                        token,
                        fingerprint,
                        displayname,
                        int(enabled),
                        int(enabled_time),
                        int(enabled_wl),
                        expires_at,
                        bw_limit_gb,
                        wl_limit_gb,
                        ext_username,
                        ext_password_hash,
                        int(time.time()),
                    ),
                )
                if pending_sync:
                    await conn.execute(
                        "INSERT INTO app_metadata(key, value) VALUES (?, ?)",
                        (f"user_create_pending:{username}", uuid),
                    )
        except DatabaseError as exc:
            if "UNIQUE" in str(exc) or "PRIMARY KEY" in str(exc):
                raise DuplicateError(
                    "username, UUID, token or external username already exists",
                ) from exc
            raise

    async def set_user(self, username: str, **fields: Unpack[UserFields]) -> None:
        mapping = {
            "userid": "uuid",
            "uuid": "uuid",
            "token": "token",
            "fingerprint": "fingerprint",
            "displayname": "displayname",
            "status": "enabled",
            "status_time": "enabled_time",
            "status_wl": "enabled_wl",
            "bw_limit": "bw_limit_gb",
            "bw_used": "bw_used",
            "wl_bw_limit": "wl_limit_gb",
            "wl_bw_used": "wl_used",
            "expiry_time": "expires_at",
            "ext_username": "ext_username",
            "ext_password": "ext_password_hash",
            "tgid": None,
        }
        updates = {mapping[k]: v for k, v in fields.items() if mapping.get(k)}
        updates = {
            k: int(bool(v)) if k in {"enabled", "enabled_time", "enabled_wl"} else v
            for k, v in updates.items()
        }
        try:
            async with self.transaction(immediate=True) as conn:
                if updates:
                    assignments = ", ".join(f"{key} = ?" for key in updates)
                    await conn.execute(
                        f"UPDATE users SET {assignments} WHERE username = ?",
                        (*updates.values(), username),
                    )
                if "tgid" in fields:
                    telegram_id = fields["tgid"]
                    await set_telegram_mapping(
                        conn,
                        username,
                        None if telegram_id is None else str(telegram_id),
                    )
        except DatabaseError as exc:
            if "UNIQUE" in str(exc):
                raise DuplicateError("unique user field already exists") from exc
            raise

    async def update_user(self, username: str, **fields: Unpack[UserFields]) -> None:
        await self.set_user(username, **fields)

    async def set_enabled_pending(
        self,
        username: str,
        *,
        enabled: bool | None,
        enabled_time: bool | None,
        enabled_wl: bool | None,
    ) -> None:
        """Publish desired flags and retry identity in one short transaction."""
        async with self.transaction(immediate=True) as conn:
            for column, value in (
                ("enabled", enabled),
                ("enabled_time", enabled_time if enabled is not None else None),
                ("enabled_wl", enabled_wl),
            ):
                if value is not None:
                    await conn.execute(
                        f"UPDATE users SET {column} = ? WHERE username = ?",
                        (int(value), username),
                    )
            await conn.execute(
                "INSERT OR REPLACE INTO app_metadata(key, value) VALUES (?, ?)",
                (f"user_sync_pending:{username}", "enabled"),
            )

    async def rollback_created_user(self, username: str, expected_uuid: str) -> None:
        async with self.transaction(immediate=True) as conn:
            row = await conn.row("SELECT uuid FROM users WHERE username = ?", (username,))
            if row is not None and row[0] != expected_uuid:
                raise DatabaseError("created-user recovery identity changed")
            await conn.execute("DELETE FROM users WHERE username = ?", (username,))
            await conn.executemany(
                "DELETE FROM app_metadata WHERE key = ?",
                [(f"user_create_pending:{username}",), (f"user_sync_pending:{username}",)],
            )

    async def finalize_reset(self, username: str, expected_uuid: str, token: str) -> None:
        """Publish reset credentials and remove intent atomically after UUID sync."""
        async with self.transaction(immediate=True) as conn:
            count = await conn.execute(
                "UPDATE users SET token = ?, auth_token = NULL WHERE username = ? AND uuid = ?",
                (token, username, expected_uuid),
            )
            if count != 1:
                raise DatabaseError("reset UUID has not committed")
            await conn.execute(
                "DELETE FROM app_metadata WHERE key = ?",
                (f"user_reset_pending:{username}",),
            )

    async def delete_user(self, username: str) -> None:
        async with self.transaction(immediate=True) as conn:
            await conn.execute("DELETE FROM users WHERE username = ?", (username,))

    async def token_to_user(self, token: str) -> str | None:
        async with self.connection() as conn:
            row = await conn.row("SELECT username FROM users WHERE token = ?", (token,))
            return str(row[0]) if row else None

    async def auth_token_to_user(self, auth_token: str) -> str | None:
        async with self.connection() as conn:
            row = await conn.row("SELECT username FROM users WHERE auth_token = ?", (auth_token,))
            return str(row[0]) if row else None

    async def set_auth_token(self, username: str, auth_token: str | None) -> None:
        async with self.transaction(immediate=True) as conn:
            count = await conn.execute(
                "UPDATE users SET auth_token = ? WHERE username = ?",
                (auth_token, username),
            )
            if count != 1:
                raise DatabaseError("failed to set auth token")

    async def ext_to_user(self, ext_username: str) -> str | None:
        async with self.connection() as conn:
            row = await conn.row(
                "SELECT username FROM users WHERE ext_username = ?",
                (ext_username,),
            )
            return str(row[0]) if row else None

    async def user_to_ext(self, username: str) -> str | None:
        async with self.connection() as conn:
            row = await conn.row("SELECT ext_username FROM users WHERE username = ?", (username,))
            return str(row[0]) if row and row[0] is not None else None

    async def ext_password(self, ext_username: str) -> str | None:
        async with self.connection() as conn:
            row = await conn.row(
                "SELECT ext_password_hash FROM users WHERE ext_username = ?",
                (ext_username,),
            )
            return str(row[0]) if row and row[0] is not None else None
