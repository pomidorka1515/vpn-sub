from __future__ import annotations

import time
from typing import cast

from custom_types import BonusResult, CodeRecord, RegisterCodeResult
from errors import CodeError, DatabaseError, DuplicateError

from .common import ConnectionMixin, row_dict

class CodesMixin(ConnectionMixin):
    def add_code(self, code: str, action: str, *, permanent: bool = False, uses: int = 1,
                 days: int = 0, gb: int = 0, wl_gb: int = 0) -> None:
        stored_uses = -1 if permanent else uses
        try:
            with self.transaction(immediate=True) as conn:
                conn.execute(
                    "INSERT INTO codes(code, action, permanent, uses, days, gb, wl_gb, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (code, action, int(permanent), stored_uses, days, gb, wl_gb, int(time.time())),
                )
        except DatabaseError as exc:
            if "UNIQUE" in str(exc):
                raise DuplicateError(f"code '{code}' already exists") from exc
            raise

    def get_code(self, code: str) -> CodeRecord | None:
        with self.connection() as conn:
            row = row_dict(conn.execute("SELECT * FROM codes WHERE code = ? AND (permanent = 1 OR uses > 0)", (code,)).fetchone())
            if row is not None:
                row["perma"] = bool(row.pop("permanent"))
            return cast(CodeRecord, row)

    def all_codes(self) -> list[CodeRecord]:
        with self.connection() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM codes WHERE permanent = 1 OR uses > 0 ORDER BY code")]
        for row in rows:
            row["perma"] = bool(row.pop("permanent"))
        return cast(list[CodeRecord], rows)

    def delete_code(self, code: str) -> bool:
        with self.transaction(immediate=True) as conn:
            cur = conn.execute("DELETE FROM codes WHERE code = ?", (code,))
            return cur.rowcount == 1

    def consume_code(self, code: str, action: str) -> CodeRecord:
        """Atomically validate and consume one finite-use code."""
        with self.transaction(immediate=True) as conn:
            row = conn.execute("SELECT * FROM codes WHERE code = ?", (code,)).fetchone()
            if row is None or row["action"] != action:
                raise CodeError("invalid code")
            if not row["permanent"] and int(row["uses"]) < 1:
                raise CodeError("invalid code")
            remaining = -1 if row["permanent"] else int(row["uses"]) - 1
            if remaining == 0:
                conn.execute("DELETE FROM codes WHERE code = ?", (code,))
            else:
                conn.execute("UPDATE codes SET uses = ? WHERE code = ?", (remaining, code))
            result = dict(row)
            result["perma"] = bool(result.pop("permanent"))
            result["uses"] = remaining
            return cast(CodeRecord, result)

    def register_with_code(
        self, *, code: str, username: str, uuid: str, token: str,
        fingerprint: str, displayname: str, ext_username: str,
        ext_password_hash: str,
    ) -> RegisterCodeResult:
        """Consume a registration code and insert the user in one transaction."""
        try:
            with self.transaction(immediate=True) as conn:
                row = conn.execute("SELECT * FROM codes WHERE code = ? AND action = 'register'", (code,)).fetchone()
                if row is None or (not row["permanent"] and int(row["uses"]) < 1):
                    raise CodeError("invalid code")
                remaining = -1 if row["permanent"] else int(row["uses"]) - 1
                conn.execute("UPDATE codes SET uses = ? WHERE code = ?", (remaining, code))
                expires_at = int(time.time() + int(row["days"]) * 86400) if int(row["days"]) else 0
                conn.execute("""INSERT INTO users
                    (username, uuid, token, fingerprint, displayname, expires_at,
                     bw_limit_gb, wl_limit_gb, ext_username, ext_password_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (username, uuid, token, fingerprint, displayname, expires_at,
                     int(row["gb"]), int(row["wl_gb"]), ext_username,
                     ext_password_hash, int(time.time())))
                conn.execute(
                    "INSERT INTO pending_registration_sync(username, code, permanent) VALUES (?, ?, ?)",
                    (username, code, int(row["permanent"])),
                )
                return {"days": int(row["days"]), "gb": int(row["gb"]),
                        "wl_gb": int(row["wl_gb"]), "uses": remaining,
                        "perma": bool(row["permanent"]), "time": expires_at}
        except DatabaseError as exc:
            if "UNIQUE" in str(exc):
                raise DuplicateError("username, UUID, token or external username already exists") from exc
            raise

    def adjust_bonus(self, username: str, code: str) -> BonusResult:
        """Consume a bonus code and update quota/time in one transaction."""
        with self.transaction(immediate=True) as conn:
            user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if user is None:
                raise CodeError("unknown user")
            code_row = conn.execute("SELECT * FROM codes WHERE code = ? AND action = 'bonus'", (code,)).fetchone()
            if code_row is None or (not code_row["permanent"] and int(code_row["uses"]) < 1):
                raise CodeError("unknown code")
            remaining = -1 if code_row["permanent"] else int(code_row["uses"]) - 1
            if remaining == 0:
                conn.execute("DELETE FROM codes WHERE code = ?", (code,))
            else:
                conn.execute("UPDATE codes SET uses = ? WHERE code = ?", (remaining, code))
            current_time = int(user["expires_at"])
            new_time = 0 if current_time == 0 else current_time + int(code_row["days"]) * 86400
            new_limit = 0 if int(user["bw_limit_gb"]) == 0 else int(user["bw_limit_gb"]) + int(code_row["gb"])
            new_wl_limit = 0 if int(user["wl_limit_gb"]) == 0 else int(user["wl_limit_gb"]) + int(code_row["wl_gb"])
            conn.execute("UPDATE users SET expires_at = ?, bw_limit_gb = ?, wl_limit_gb = ? WHERE username = ?",
                         (new_time, new_limit, new_wl_limit, username))
            return {
                "days": int(code_row["days"]), "gb": int(code_row["gb"]),
                "wl_gb": int(code_row["wl_gb"]), "perma": bool(code_row["permanent"]),
                "uses": remaining, "time": new_time, "limit": new_limit,
                "wl_limit": new_wl_limit,
            }

    def confirm_registration_sync(self, username: str) -> None:
        with self.transaction(immediate=True) as conn:
            row = conn.execute("SELECT code FROM pending_registration_sync WHERE username = ?", (username,)).fetchone()
            conn.execute("DELETE FROM pending_registration_sync WHERE username = ?", (username,))
            if row is not None:
                conn.execute("DELETE FROM codes WHERE code = ? AND permanent = 0 AND uses = 0", (str(row["code"]),))

    def rollback_registration_sync(self, username: str) -> None:
        """Remove a locally-created user and refund its finite register code."""
        with self.transaction(immediate=True) as conn:
            pending = conn.execute(
                "SELECT code, permanent FROM pending_registration_sync WHERE username = ?", (username,)
            ).fetchone()
            if pending is not None and not bool(pending["permanent"]):
                conn.execute(
                    "UPDATE codes SET uses = uses + 1 WHERE code = ? AND action = 'register' AND permanent = 0",
                    (str(pending["code"]),),
                )
            conn.execute("DELETE FROM users WHERE username = ?", (username,))
