from __future__ import annotations

import time
from typing import cast

from custom_types import UserRecord
from errors import DatabaseError, DuplicateError

from .common import ConnectionMixin
from .telegram import set_telegram_mapping

class UsersMixin(ConnectionMixin):
    def get_user(self, username: str) -> UserRecord | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            return cast(UserRecord, dict(row)) if row is not None else None

    def list_users(self) -> list[str]:
        with self.connection() as conn:
            return [str(row[0]) for row in conn.execute("SELECT username FROM users ORDER BY username")]

    def all_users(self) -> list[str]:
        return self.list_users()

    def user_exists(self, username: str) -> bool:
        with self.connection() as conn:
            return conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone() is not None

    def create_user(
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
    ) -> None:
        try:
            with self.transaction(immediate=True) as conn:
                conn.execute(
                    """INSERT INTO users
                    (username, uuid, token, fingerprint, displayname, enabled,
                     enabled_time, enabled_wl, expires_at, bw_limit_gb, wl_limit_gb,
                     ext_username, ext_password_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (username, uuid, token, fingerprint, displayname, int(enabled),
                     int(enabled_time), int(enabled_wl), expires_at, bw_limit_gb,
                     wl_limit_gb, ext_username, ext_password_hash, int(time.time())),
                )
        except DatabaseError as exc:
            if "UNIQUE" in str(exc) or "PRIMARY KEY" in str(exc):
                raise DuplicateError("username, UUID, token or external username already exists") from exc
            raise

    def set_user(self, username: str, **fields: object) -> None:
        mapping = {
            "userid": "uuid", "uuid": "uuid", "token": "token",
            "fingerprint": "fingerprint", "displayname": "displayname",
            "status": "enabled", "status_time": "enabled_time", "status_wl": "enabled_wl",
            "bw_limit": "bw_limit_gb", "bw_used": "bw_used",
            "wl_bw_limit": "wl_limit_gb", "wl_bw_used": "wl_used",
            "expiry_time": "expires_at", "ext_username": "ext_username",
            "ext_password": "ext_password_hash", "tgid": None,
        }
        updates = {mapping[k]: v for k, v in fields.items() if k in mapping and mapping[k]}
        updates = {k: int(bool(v)) if k in ("enabled", "enabled_time", "enabled_wl") else v for k, v in updates.items()}
        try:
            with self.transaction(immediate=True) as conn:
                if updates:
                    assignments = ", ".join(f"{key} = ?" for key in updates)
                    conn.execute(f"UPDATE users SET {assignments} WHERE username = ?", (*updates.values(), username))
                if "tgid" in fields:
                    telegram_id = fields["tgid"]
                    set_telegram_mapping(
                        conn, username,
                        None if telegram_id is None else str(telegram_id),
                    )
        except DatabaseError as exc:
            if "UNIQUE" in str(exc):
                raise DuplicateError("unique user field already exists") from exc
            raise

    def update_user(self, username: str, **fields: object) -> None:
        self.set_user(username, **fields)

    def delete_user(self, username: str) -> None:
        with self.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM users WHERE username = ?", (username,))

    def token_to_user(self, token: str) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT username FROM users WHERE token = ?", (token,)).fetchone()
            return str(row[0]) if row else None

    def auth_token_to_user(self, auth_token: str) -> str | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT username FROM users WHERE auth_token = ?", (auth_token,)
            ).fetchone()
            return str(row[0]) if row else None

    def set_auth_token(self, username: str, auth_token: str | None) -> None:
        with self.transaction(immediate=True) as conn:
            cursor = conn.execute(
                "UPDATE users SET auth_token = ? WHERE username = ?", (auth_token, username)
            )
            if cursor.rowcount != 1:
                raise DatabaseError("failed to set auth token")

    def ext_to_user(self, ext_username: str) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT username FROM users WHERE ext_username = ?", (ext_username,)).fetchone()
            return str(row[0]) if row else None

    def user_to_ext(self, username: str) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT ext_username FROM users WHERE username = ?", (username,)).fetchone()
            return str(row[0]) if row and row[0] is not None else None

    def ext_password(self, ext_username: str) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT ext_password_hash FROM users WHERE ext_username = ?", (ext_username,)).fetchone()
            return str(row[0]) if row and row[0] is not None else None
