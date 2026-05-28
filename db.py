from __future__ import annotations

import sqlite3

__all__ = ["Database"]

class Database:
    """
    SQLite store for dynamic runtime data (users, codes, reverse lookups).
    Static config stays in config.py.
    """
    def __init__(self, *, path: str, timeout: int = 5) -> None:
        self._conn = sqlite3.connect(path, timeout=timeout)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        """Create tables if they don't exist."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                userid TEXT NOT NULL,
                token TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                displayname TEXT NOT NULL,
                status INTEGER DEFAULT 1,
                status_wl INTEGER DEFAULT 1,
                status_time INTEGER DEFAULT 1,
                bw_limit INTEGER DEFAULT 0,
                bw_used INTEGER DEFAULT 0,
                wl_bw_limit INTEGER DEFAULT 0,
                wl_bw_used INTEGER DEFAULT 0,
                expiry_time INTEGER DEFAULT 0,
                ext_username TEXT,
                ext_password TEXT,
                tgid TEXT
            );
            CREATE TABLE IF NOT EXISTS codes (
                code TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                perma INTEGER DEFAULT 0,
                days INTEGER DEFAULT 0,
                gb INTEGER DEFAULT 0,
                wl_gb INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_users_token ON users(token);
            CREATE INDEX IF NOT EXISTS idx_users_ext ON users(ext_username);
            CREATE INDEX IF NOT EXISTS idx_users_tgid ON users(tgid);
        """)
        self._conn.commit()

    # --- User helpers ---

    def get_user(self, username: str) -> dict[str, object] | None:
        """Get all data for a user. Returns None if not found."""
        cur = self._conn.execute("SELECT * FROM users WHERE username = ?", (username,))
        row = cur.fetchone()
        return dict(row) if row else None

    def set_user(self, username: str, **fields: object) -> None:
        """Set fields on a user (insert or update)."""
        allowed = {
            'userid', 'token', 'fingerprint', 'displayname', 'status',
            'status_wl', 'status_time', 'bw_limit', 'bw_used',
            'wl_bw_limit', 'wl_bw_used', 'expiry_time',
            'ext_username', 'ext_password', 'tgid'
        }
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        cols = list(fields.keys())
        vals = list(fields.values())
        self._conn.execute(
            f"INSERT INTO users (username, {', '.join(cols)}) "
            f"VALUES (?, {', '.join(['?'] * len(cols))}) "
            f"ON CONFLICT(username) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in cols)}",
            [username] + vals
        )
        self._conn.commit()

    def del_user(self, username: str) -> None:
        """Delete a user."""
        self._conn.execute("DELETE FROM users WHERE username = ?", (username,))
        self._conn.commit()

    def user_exists(self, username: str) -> bool:
        """Check if user exists."""
        cur = self._conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (username,)
        )
        return cur.fetchone() is not None

    def all_users(self) -> list[str]:
        """Get all usernames."""
        cur = self._conn.execute("SELECT username FROM users")
        return [row[0] for row in cur.fetchall()]

    # --- Code helpers ---

    def get_code(self, code: str) -> dict[str, object] | None:
        """Get a code. Returns None if not found."""
        cur = self._conn.execute("SELECT * FROM codes WHERE code = ?", (code,))
        row = cur.fetchone()
        return dict(row) if row else None

    def add_code(self, code: str, action: str, perma: bool = False,
                 days: int = 0, gb: int = 0, wl_gb: int = 0) -> None:
        """Add a code."""
        self._conn.execute(
            "INSERT OR REPLACE INTO codes (code, action, perma, days, gb, wl_gb) VALUES (?, ?, ?, ?, ?, ?)",
            (code, action, 1 if perma else 0, days, gb, wl_gb)
        )
        self._conn.commit()

    def del_code(self, code: str) -> None:
        """Delete a code."""
        self._conn.execute("DELETE FROM codes WHERE code = ?", (code,))
        self._conn.commit()

    def all_codes(self) -> list[dict[str, object]]:
        """Get all codes as list of dicts."""
        cur = self._conn.execute("SELECT * FROM codes")
        return [dict(row) for row in cur.fetchall()]

    # --- Reverse lookups ---

    def token_to_user(self, token: str) -> str | None:
        """Get username by token."""
        cur = self._conn.execute(
            "SELECT username FROM users WHERE token = ?", (token,)
        )
        row = cur.fetchone()
        return row[0] if row else None

    def tgid_to_user(self, tgid: str) -> str | None:
        """Get username by telegram ID."""
        cur = self._conn.execute(
            "SELECT username FROM users WHERE tgid = ?", (tgid,)
        )
        row = cur.fetchone()
        return row[0] if row else None

    def user_to_tgid(self, username: str) -> str | None:
        """Get telegram ID by username."""
        cur = self._conn.execute(
            "SELECT tgid FROM users WHERE username = ?", (username,)
        )
        row = cur.fetchone()
        return row[0] if row else None

    def ext_to_user(self, ext_username: str) -> str | None:
        """Get internal username by external username."""
        cur = self._conn.execute(
            "SELECT username FROM users WHERE ext_username = ?", (ext_username,)
        )
        row = cur.fetchone()
        return row[0] if row else None

    def user_to_ext(self, username: str) -> str | None:
        """Get external username by internal username."""
        cur = self._conn.execute(
            "SELECT ext_username FROM users WHERE username = ?", (username,)
        )
        row = cur.fetchone()
        return row[0] if row else None

    def ext_password(self, ext_username: str) -> str | None:
        """Get hashed password for external username."""
        cur = self._conn.execute(
            "SELECT ext_password FROM users WHERE ext_username = ?", (ext_username,)
        )
        row = cur.fetchone()
        return row[0] if row else None

    def close(self) -> None:
        """Close the connection."""
        self._conn.close()