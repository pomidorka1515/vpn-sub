from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

from custom_types import CodeRecord
from errors import CodeError, DatabaseError, DuplicateError

from .async_common import AsyncConnectionMixin

if TYPE_CHECKING:
    from custom_types import BonusResult, RegisterCodeResult


class AsyncCodesMixin(AsyncConnectionMixin):
    async def add_code(self, code: str, action: str, *, permanent: bool = False, uses: int = 1, days: int = 0, gb: int = 0, wl_gb: int = 0) -> None:
        try:
            async with self.transaction(immediate=True) as conn:
                await conn.execute("INSERT INTO codes(code, action, permanent, uses, days, gb, wl_gb, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (code, action, int(permanent), -1 if permanent else uses, days, gb, wl_gb, int(time.time())))
        except DatabaseError as exc:
            if "UNIQUE" in str(exc):
                raise DuplicateError(f"code '{code}' already exists") from exc
            raise

    async def get_code(self, code: str) -> CodeRecord | None:
        async with self.connection() as conn:
            row = await conn.row("SELECT * FROM codes WHERE code = ? AND (permanent = 1 OR uses > 0)", (code,))
            if row is None:
                return None
            result = dict(row)
            result["perma"] = bool(result.pop("permanent"))
            return cast(CodeRecord, result)

    async def all_codes(self) -> list[CodeRecord]:
        async with self.connection() as conn:
            rows = [dict(row) for row in await conn.rows("SELECT * FROM codes WHERE permanent = 1 OR uses > 0 ORDER BY code")]
        for row in rows:
            row["perma"] = bool(row.pop("permanent"))
        return cast(list[CodeRecord], rows)

    async def delete_code(self, code: str) -> bool:
        async with self.transaction(immediate=True) as conn:
            return await conn.execute("DELETE FROM codes WHERE code = ?", (code,)) == 1

    async def consume_code(self, code: str, action: str) -> CodeRecord:
        async with self.transaction(immediate=True) as conn:
            row = await conn.row("SELECT * FROM codes WHERE code = ?", (code,))
            if row is None or row["action"] != action or (not row["permanent"] and int(row["uses"]) < 1):
                raise CodeError("invalid code")
            remaining = -1 if row["permanent"] else int(row["uses"]) - 1
            if remaining == 0:
                await conn.execute("DELETE FROM codes WHERE code = ?", (code,))
            else:
                await conn.execute("UPDATE codes SET uses = ? WHERE code = ?", (remaining, code))
            result = dict(row)
            result["perma"] = bool(result.pop("permanent"))
            result["uses"] = remaining
            return cast(CodeRecord, result)

    async def register_with_code(self, *, code: str, username: str, uuid: str, token: str, fingerprint: str, displayname: str, ext_username: str, ext_password_hash: str) -> RegisterCodeResult:
        try:
            async with self.transaction(immediate=True) as conn:
                row = await conn.row("SELECT * FROM codes WHERE code = ? AND action = 'register'", (code,))
                if row is None or (not row["permanent"] and int(row["uses"]) < 1):
                    raise CodeError("invalid code")
                remaining = -1 if row["permanent"] else int(row["uses"]) - 1
                await conn.execute("UPDATE codes SET uses = ? WHERE code = ?", (remaining, code))
                expires_at = int(time.time() + int(row["days"]) * 86400) if int(row["days"]) else 0
                await conn.execute(
                    """INSERT INTO users
                    (username, uuid, token, fingerprint, displayname, expires_at,
                     bw_limit_gb, wl_limit_gb, ext_username, ext_password_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (username, uuid, token, fingerprint, displayname, expires_at, int(row["gb"]), int(row["wl_gb"]), ext_username, ext_password_hash, int(time.time())),
                )
                await conn.execute("INSERT INTO pending_registration_sync(username, code, permanent) VALUES (?, ?, ?)", (username, code, int(row["permanent"])))
                return {"days": int(row["days"]), "gb": int(row["gb"]), "wl_gb": int(row["wl_gb"]), "uses": remaining, "perma": bool(row["permanent"]), "time": expires_at}
        except DatabaseError as exc:
            if "UNIQUE" in str(exc):
                raise DuplicateError("username, UUID, token or external username already exists") from exc
            raise

    async def adjust_bonus(self, username: str, code: str) -> BonusResult:
        async with self.transaction(immediate=True) as conn:
            user = await conn.row("SELECT * FROM users WHERE username = ?", (username,))
            if user is None:
                raise CodeError("unknown user")
            row = await conn.row("SELECT * FROM codes WHERE code = ? AND action = 'bonus'", (code,))
            if row is None or (not row["permanent"] and int(row["uses"]) < 1):
                raise CodeError("unknown code")
            remaining = -1 if row["permanent"] else int(row["uses"]) - 1
            if remaining == 0:
                await conn.execute("DELETE FROM codes WHERE code = ?", (code,))
            else:
                await conn.execute("UPDATE codes SET uses = ? WHERE code = ?", (remaining, code))
            now = int(time.time())
            days = int(row["days"])
            current = int(user["expires_at"])
            new_time = current if current == 0 or days == 0 else max(current, now) + days * 86400
            limit = int(user["bw_limit_gb"]) + int(row["gb"])
            wl_limit = int(user["wl_limit_gb"]) + int(row["wl_gb"])
            await conn.execute("UPDATE users SET expires_at = ?, bw_limit_gb = ?, wl_limit_gb = ? WHERE username = ?", (new_time, limit, wl_limit, username))
            return {"days": days, "gb": int(row["gb"]), "wl_gb": int(row["wl_gb"]), "perma": bool(row["permanent"]), "uses": remaining, "time": new_time, "limit": limit, "wl_limit": wl_limit}

    async def confirm_registration_sync(self, username: str) -> None:
        async with self.transaction(immediate=True) as conn:
            row = await conn.row("SELECT code FROM pending_registration_sync WHERE username = ?", (username,))
            await conn.execute("DELETE FROM pending_registration_sync WHERE username = ?", (username,))
            if row is not None:
                await conn.execute("DELETE FROM codes WHERE code = ? AND permanent = 0 AND uses = 0", (str(row["code"]),))

    async def rollback_registration_sync(self, username: str) -> None:
        async with self.transaction(immediate=True) as conn:
            row = await conn.row("SELECT code, permanent FROM pending_registration_sync WHERE username = ?", (username,))
            if row is not None and not bool(row["permanent"]):
                await conn.execute("UPDATE codes SET uses = uses + 1 WHERE code = ? AND action = 'register' AND permanent = 0", (str(row["code"]),))
            await conn.execute("DELETE FROM users WHERE username = ?", (username,))
