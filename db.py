"""SQLite persistence for dynamic subscription state.

The JSON configuration contains deployment and presentation settings. This module owns mutable user, code, quota, Telegram, notification and snapshot state.
Connections are deliberately short lived: every operation obtains a connection for its transaction, which is safe for Flask and background threads.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Generator, Mapping, Sequence
from typing import Any, cast, TypedDict

import jsonschema

try:
    import fcntl
except ModuleNotFoundError as exc:
    raise RuntimeError("Run this on Linux.") from exc

__all__ = [
    "Database", "DatabaseError", "DuplicateError", "CodeError",
    "MigrationError", "MigrationReport", "UserRecord", "CodeRecord",
    "RegisterCodeResult", "BonusResult", "migrate_legacy",
]


class UserRecord(TypedDict):
    username: str
    uuid: str
    token: str
    fingerprint: str
    displayname: str
    enabled: int
    enabled_time: int
    enabled_wl: int
    expires_at: int
    bw_limit_gb: int
    bw_used: int
    wl_limit_gb: int
    wl_used: int
    ext_username: str | None
    ext_password_hash: str | None
    created_at: int


class CodeRecord(TypedDict):
    code: str
    action: str
    perma: bool
    uses: int
    days: int
    gb: int
    wl_gb: int
    created_at: int


class RegisterCodeResult(TypedDict):
    days: int
    gb: int
    wl_gb: int
    uses: int
    perma: bool
    time: int


class BonusResult(RegisterCodeResult):
    limit: int
    wl_limit: int


class DatabaseError(RuntimeError):
    """A storage failure, distinct from an expected business-rule failure."""


class DuplicateError(DatabaseError):
    """A unique user or credential value is already in use."""


class CodeError(DatabaseError):
    """An invalid, expired or exhausted registration/bonus code."""


class MigrationError(DatabaseError):
    """Legacy data could not be validated or imported."""


@dataclass(frozen=True, slots=True)
class MigrationReport:
    users: int
    codes: int
    telegram_mappings: int
    bandwidth_snapshots: int
    state_snapshots: int
    backup_paths: tuple[str, ...]
    skipped_orphans: int = 0
    already_migrated: bool = False


# ``json.load`` returns ``Any``.  keep the migration boundary explicit so a malformed legacy document is rejected instead of leaking ``Any`` through
LegacyObject = Mapping[str, object]
LegacyUserRow = tuple[
    str, str, str, str, str, int, int, int, int, int, int, int, int,
    str | None, str | None,
]


def _legacy_object(value: object, label: str) -> LegacyObject:
    if not isinstance(value, Mapping):
        raise MigrationError(f"{label} must be an object")
    return cast(LegacyObject, value)


def _legacy_map(config: LegacyObject, key: str) -> LegacyObject:
    value = config.get(key, {})
    return _legacy_object(value, f"config.{key}")


def _legacy_int(value: object, label: str, *, minimum: int | None = None) -> int:
    # ``bool`` is a subclass of ``int`` but is not a schema integer
    if not isinstance(value, int) or isinstance(value, bool):
        raise MigrationError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise MigrationError(f"{label} must be >= {minimum}")
    return value


def _legacy_bool(value: object, label: str, *, default: bool | None = None) -> bool:
    if value is None and default is not None:
        return default
    if not isinstance(value, bool):
        raise MigrationError(f"{label} must be a boolean")
    return value


def _legacy_string(value: object, label: str, *, default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    if not isinstance(value, str):
        raise MigrationError(f"{label} must be a string")
    return value


def _legacy_map_string(
    mapping: LegacyObject,
    key: str,
    label: str,
    *,
    default: str | None = None,
) -> str:
    if key not in mapping:
        if default is None:
            raise MigrationError(f"{label} is missing")
        return default
    value = mapping[key]
    return _legacy_string(value, label)


def _legacy_map_bool(
    mapping: LegacyObject,
    key: str,
    label: str,
    *,
    default: bool,
) -> bool:
    value = mapping.get(key, default)
    return _legacy_bool(value, label)


def _legacy_map_int(
    mapping: LegacyObject,
    key: str,
    label: str,
    *,
    default: int,
    minimum: int | None = None,
) -> int:
    value = mapping.get(key, default)
    return _legacy_int(value, label, minimum=minimum)


def _legacy_quota(value: object, label: str) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)):
        raise MigrationError(f"{label} must contain exactly [limit, used]")
    quota = cast(Sequence[object], value)
    if len(quota) != 2:
        raise MigrationError(f"{label} must contain exactly [limit, used]")
    limit = _legacy_int(quota[0], f"{label}[0]", minimum=0)
    used = _legacy_int(quota[1], f"{label}[1]", minimum=0)
    return limit, used


class Database:
    """Thread-safe SQLite repository for dynamic application state."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str, timeout: float = 5.0) -> None:
        self.path = os.path.abspath(path)
        self.timeout = timeout
        self._connections: set[sqlite3.Connection] = set()
        self._connections_lock = threading.Lock()
        parent = os.path.dirname(self.path)
        try:
            if parent:
                os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise DatabaseError(f"unable to create database directory {parent}: {exc}") from exc
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                self.path,
                timeout=self.timeout,
                isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {max(1, int(self.timeout * 1000))}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            with self._connections_lock:
                self._connections.add(conn)
            return conn
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            raise DatabaseError(f"unable to open database {self.path}: {exc}") from exc

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self._connect()
        try:
            yield conn
        except sqlite3.Error as exc:
            raise DatabaseError(str(exc)) from exc
        finally:
            with self._connections_lock:
                self._connections.discard(conn)
            conn.close()

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Generator[sqlite3.Connection, None, None]:
        with self.connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield conn
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise DatabaseError(str(exc)) from exc
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def initialize(self) -> None:
        """Apply the versioned schema migration exactly once."""
        with self.connection() as conn:
            try:
                # keep schema creation in one explicit transaction; ``executescript`` would commit
                # before running and could leave a half-created schema after
                # an interrupted migration.
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS schema_version (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        version INTEGER NOT NULL
                    )"""
                )
                conn.execute("INSERT INTO schema_version(id, version) VALUES (1, 0) ON CONFLICT(id) DO NOTHING")
                version_row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
                if version_row is None:
                    raise MigrationError("schema_version row is missing")
                try:
                    version = int(version_row[0])
                except (TypeError, ValueError) as exc:
                    raise MigrationError("schema version is not an integer") from exc
                if version < 0:
                    raise MigrationError(f"invalid database schema version {version}")
                if version > self.SCHEMA_VERSION:
                    raise MigrationError(f"database schema {version} is newer than supported {self.SCHEMA_VERSION}")
                if version == 0:
                    existing_tables = {
                        str(row[0]) for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name != 'schema_version'"
                        )
                    }
                    if existing_tables:
                        names = ", ".join(sorted(existing_tables))
                        raise MigrationError(f"unversioned database contains existing tables: {names}")
                    schema_statements = (
                        """CREATE TABLE users (
                            username TEXT PRIMARY KEY,
                            uuid TEXT NOT NULL UNIQUE,
                            token TEXT NOT NULL UNIQUE,
                            fingerprint TEXT NOT NULL,
                            displayname TEXT NOT NULL,
                            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                            enabled_time INTEGER NOT NULL DEFAULT 1 CHECK (enabled_time IN (0, 1)),
                            enabled_wl INTEGER NOT NULL DEFAULT 1 CHECK (enabled_wl IN (0, 1)),
                            expires_at INTEGER NOT NULL DEFAULT 0 CHECK (expires_at >= 0),
                            bw_limit_gb INTEGER NOT NULL DEFAULT 0 CHECK (bw_limit_gb >= 0),
                            bw_used INTEGER NOT NULL DEFAULT 0 CHECK (bw_used >= 0),
                            wl_limit_gb INTEGER NOT NULL DEFAULT 0 CHECK (wl_limit_gb >= 0),
                            wl_used INTEGER NOT NULL DEFAULT 0 CHECK (wl_used >= 0),
                            ext_username TEXT UNIQUE,
                            ext_password_hash TEXT,
                            created_at INTEGER NOT NULL DEFAULT 0
                        )""",
                        "CREATE INDEX idx_users_token ON users(token)",
                        "CREATE INDEX idx_users_ext_username ON users(ext_username)",
                        """CREATE TABLE telegram_mappings (
                            telegram_id TEXT PRIMARY KEY,
                            username TEXT NOT NULL UNIQUE REFERENCES users(username) ON DELETE CASCADE
                        )""",
                        """CREATE TABLE telegram_preferences (
                            telegram_id TEXT PRIMARY KEY,
                            language TEXT NOT NULL CHECK (language IN ('ru', 'en'))
                        )""",
                        """CREATE TABLE codes (
                            code TEXT PRIMARY KEY,
                            action TEXT NOT NULL CHECK (action IN ('register', 'bonus')),
                            permanent INTEGER NOT NULL DEFAULT 0 CHECK (permanent IN (0, 1)),
                            uses INTEGER NOT NULL CHECK (uses = -1 OR uses >= 0),
                            days INTEGER NOT NULL DEFAULT 0 CHECK (days >= 0),
                            gb INTEGER NOT NULL DEFAULT 0 CHECK (gb >= 0),
                            wl_gb INTEGER NOT NULL DEFAULT 0 CHECK (wl_gb >= 0),
                            created_at INTEGER NOT NULL DEFAULT 0
                        )""",
                        """CREATE TABLE pending_registration_sync (
                            username TEXT PRIMARY KEY REFERENCES users(username) ON DELETE CASCADE,
                            code TEXT NOT NULL REFERENCES codes(code),
                            permanent INTEGER NOT NULL CHECK (permanent IN (0, 1))
                        )""",
                        """CREATE TABLE notification_state (
                            kind TEXT NOT NULL,
                            telegram_id TEXT NOT NULL,
                            PRIMARY KEY(kind, telegram_id)
                        )""",
                        """CREATE TABLE app_metadata (
                            key TEXT PRIMARY KEY,
                            value TEXT NOT NULL
                        )""",
                        """CREATE TABLE bandwidth_snapshots (
                            username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                            ts INTEGER NOT NULL,
                            up INTEGER NOT NULL DEFAULT 0,
                            down INTEGER NOT NULL DEFAULT 0,
                            wl_up INTEGER NOT NULL DEFAULT 0,
                            wl_down INTEGER NOT NULL DEFAULT 0,
                            PRIMARY KEY(username, ts)
                        )""",
                        "CREATE INDEX idx_bandwidth_snapshots_ts ON bandwidth_snapshots(ts DESC)",
                        """CREATE TABLE state_snapshots (
                            ts INTEGER PRIMARY KEY,
                            payload TEXT NOT NULL
                        )""",
                    )
                    for statement in schema_statements:
                        conn.execute(statement)
                    conn.execute("UPDATE schema_version SET version = ? WHERE id = 1", (self.SCHEMA_VERSION,))
                conn.execute("COMMIT")
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, object] | None:
        return dict(row) if row is not None else None

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
                    self._set_telegram_connection(
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

    @staticmethod
    def _set_telegram_connection(
        conn: sqlite3.Connection,
        username: str,
        telegram_id: str | None,
    ) -> None:
        if telegram_id is None:
            conn.execute("DELETE FROM telegram_mappings WHERE username = ?", (username,))
            return
        # a login reassigns both sides of the one-to-one mapping
        # matching the old table's last-login-wins behavior without stale reverse rows
        conn.execute(
            "DELETE FROM telegram_mappings WHERE telegram_id = ? OR username = ?",
            (telegram_id, username),
        )
        conn.execute(
            "INSERT INTO telegram_mappings(telegram_id, username) VALUES (?, ?)",
            (telegram_id, username),
        )

    def set_telegram(self, username: str, telegram_id: str | None) -> None:
        with self.transaction(immediate=True) as conn:
            self._set_telegram_connection(conn, username, telegram_id)

    def tgid_to_user(self, tgid: str | int) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT username FROM telegram_mappings WHERE telegram_id = ?", (str(tgid),)).fetchone()
            return str(row[0]) if row else None

    def user_to_tgid(self, username: str) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT telegram_id FROM telegram_mappings WHERE username = ?", (username,)).fetchone()
            return str(row[0]) if row else None

    def get_telegram_language(self, telegram_id: str | int, default: str = "ru") -> str:
        with self.connection() as conn:
            row = conn.execute("SELECT language FROM telegram_preferences WHERE telegram_id = ?", (str(telegram_id),)).fetchone()
            return str(row[0]) if row else default

    def has_telegram_language(self, telegram_id: str | int) -> bool:
        with self.connection() as conn:
            return conn.execute("SELECT 1 FROM telegram_preferences WHERE telegram_id = ?", (str(telegram_id),)).fetchone() is not None

    def set_telegram_language(self, telegram_id: str | int, language: str) -> None:
        with self.transaction(immediate=True) as conn:
            conn.execute("INSERT INTO telegram_preferences(telegram_id, language) VALUES (?, ?) ON CONFLICT(telegram_id) DO UPDATE SET language=excluded.language", (str(telegram_id), language))

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
            row = self._row(conn.execute("SELECT * FROM codes WHERE code = ? AND (permanent = 1 OR uses > 0)", (code,)).fetchone())
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

    def increment_usage(self, username: str, *, regular: int = 0, whitelist: int = 0) -> None:
        with self.transaction(immediate=True) as conn:
            conn.execute("UPDATE users SET bw_used = bw_used + ?, wl_used = wl_used + ? WHERE username = ?",
                         (regular, whitelist, username))

    def reset_monthly(self, month: str, today: str) -> bool:
        with self.transaction(immediate=True) as conn:
            current = conn.execute("SELECT value FROM app_metadata WHERE key = 'last_reset_month'").fetchone()
            if current is not None and current[0] == month:
                return False
            conn.execute("UPDATE users SET bw_used = 0, wl_used = 0")
            for key, value in (("last_reset", today), ("last_reset_month", month)):
                conn.execute("INSERT INTO app_metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
            conn.execute("DELETE FROM notification_state")
            return True

    def clear_notifications(self) -> None:
        with self.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM notification_state")

    def notification_seen(self, kind: str, telegram_id: int | str) -> bool:
        with self.connection() as conn:
            return conn.execute("SELECT 1 FROM notification_state WHERE kind = ? AND telegram_id = ?", (kind, str(telegram_id))).fetchone() is not None

    def mark_notification(self, kind: str, telegram_id: int | str) -> bool:
        with self.transaction(immediate=True) as conn:
            cur = conn.execute("INSERT OR IGNORE INTO notification_state(kind, telegram_id) VALUES (?, ?)", (kind, str(telegram_id)))
            return cur.rowcount == 1

    def get_metadata(self, key: str, default: str | None = None) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM app_metadata WHERE key = ?", (key,)).fetchone()
            return str(row[0]) if row else default

    def upsert_bandwidth_snapshot(self, username: str, ts: int, up: int, down: int, wl_up: int, wl_down: int) -> None:
        with self.transaction(immediate=True) as conn:
            conn.execute("""INSERT INTO bandwidth_snapshots(username, ts, up, down, wl_up, wl_down)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(username, ts) DO UPDATE SET
                up=excluded.up, down=excluded.down, wl_up=excluded.wl_up, wl_down=excluded.wl_down""",
                         (username, ts, up, down, wl_up, wl_down))

    def get_bandwidth_snapshots(self, username: str, cutoff: int) -> list[dict[str, int]]:
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT ts, up, down, wl_up, wl_down FROM bandwidth_snapshots WHERE username = ? AND ts >= ? ORDER BY ts DESC",
                (username, cutoff),
            )]

    def prune_bandwidth_snapshots(self, cutoff: int) -> int:
        with self.transaction(immediate=True) as conn:
            cur = conn.execute("DELETE FROM bandwidth_snapshots WHERE ts < ?", (cutoff,))
            return cur.rowcount

    def upsert_state_snapshot(self, ts: int, payload: Mapping[str, object]) -> None:
        with self.transaction(immediate=True) as conn:
            conn.execute("INSERT INTO state_snapshots(ts, payload) VALUES (?, ?) ON CONFLICT(ts) DO UPDATE SET payload=excluded.payload",
                         (ts, json.dumps(payload, separators=(",", ":"))))

    def get_state_snapshots(self, cutoff: int) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute("SELECT payload FROM state_snapshots WHERE ts >= ? ORDER BY ts DESC", (cutoff,)).fetchall()
        return [json.loads(str(row[0])) for row in rows]

    def prune_state_snapshots(self, cutoff: int) -> int:
        with self.transaction(immediate=True) as conn:
            cur = conn.execute("DELETE FROM state_snapshots WHERE ts < ?", (cutoff,))
            return cur.rowcount

    def close(self) -> None:
        with self._connections_lock:
            connections = tuple(self._connections)
            self._connections.clear()
        for conn in connections:
            conn.close()


def _json(path: str) -> object:
    try:
        with open(path, encoding="utf-8") as handle:
            loaded: object = json.load(handle)
            return loaded
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(f"cannot read legacy JSON {path}: {exc}") from exc


def _backup(path: str, backup_dir: str, stamp: str) -> str | None:
    if not os.path.exists(path):
        return None
    target = os.path.join(backup_dir, f"{os.path.basename(path)}.{stamp}.migration-backup")
    try:
        os.makedirs(backup_dir, exist_ok=True)
        shutil.copy2(path, target)
    except OSError as exc:
        raise MigrationError(f"could not back up legacy file {path}: {exc}") from exc
    return target


_LEGACY_DYNAMIC_KEYS = (
    "users", "tokens", "displaynames", "tgids", "bw", "wl_bw",
    "userFingerprints", "time", "status", "statusTime", "statusWl",
    "webui_users", "webui_passwords", "codes", "_notified", "_wl_notified",
    "_last_reset", "_last_reset_month",
)


def _remove_legacy_dynamic_keys(path: str, config: LegacyObject) -> None:
    """Atomically remove imported state so config has one source of truth."""
    publicbot_value = config.get("publicbot")
    publicbot = cast(LegacyObject, publicbot_value) if isinstance(publicbot_value, Mapping) else None
    has_language_state = publicbot is not None and "tg_lang" in publicbot
    if not any(key in config for key in _LEGACY_DYNAMIC_KEYS) and not has_language_state:
        return
    cleaned: dict[str, object] = {
        key: value for key, value in config.items() if key not in _LEGACY_DYNAMIC_KEYS
    }
    if publicbot is not None:
        cleaned["publicbot"] = {key: value for key, value in publicbot.items() if key != "tg_lang"}
    parent = os.path.dirname(os.path.abspath(path))
    schema_ref = cleaned.get("$schema")
    if isinstance(schema_ref, str):
        if schema_ref.startswith(("http://", "https://")):
            raise MigrationError("remote config schemas are not supported")
        schema_path = os.path.normpath(os.path.join(parent, schema_ref))
        try:
            if not os.path.isfile(schema_path):
                raise MigrationError(f"config schema file not found: {schema_path}")
            schema = _json(schema_path)
            if not isinstance(schema, Mapping):
                raise MigrationError("config schema must be a JSON object")
            jsonschema.validate(cleaned, cast(Mapping[str, Any], schema))
        except jsonschema.ValidationError as exc:
            raise MigrationError(f"migrated config is invalid: {exc.message}") from exc
        except jsonschema.SchemaError as exc:
            raise MigrationError(f"migrated config schema is invalid: {exc.message}") from exc
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=parent,
            prefix=".migration-",
            suffix=".json",
        )
        os.close(descriptor)
        original = os.stat(path, follow_symlinks=False)
        os.chmod(temporary, original.st_mode & 0o777)
        try:
            os.chown(temporary, original.st_uid, original.st_gid)
        except PermissionError:
            pass
    except OSError as exc:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        raise MigrationError(f"could not prepare migrated config: {exc}") from exc
    assert temporary is not None
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(cleaned, handle, indent=4, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise MigrationError(f"could not remove migrated config keys: {exc}") from exc


def _legacy_migration_complete(path: str) -> bool:
    """Check the marker without creating or changing the database."""
    if not os.path.exists(path):
        return False
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
        row = conn.execute("SELECT value FROM app_metadata WHERE key = 'legacy_migration'").fetchone()
        return row is not None and row[0] == "complete"
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            conn.close()


def migrate_legacy(
    *,
    db_path: str,
    config_path: str,
    bandwidth_path: str | None = None,
    snapshots_path: str | None = None,
    backup_dir: str | None = None,
) -> MigrationReport:
    """Import legacy JSON state once, preserving timestamped source backups.

    The marker is written in the same transaction as imported rows, so an
    interrupted import can be retried without duplicating state. Historical
    source files are retained; imported keys are removed from config.
    """
    lock_path = f"{os.path.abspath(db_path)}.migration.lock"
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, "a+b") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            try:
                return _migrate_legacy_locked(
                    db_path=db_path, config_path=config_path,
                    bandwidth_path=bandwidth_path, snapshots_path=snapshots_path,
                    backup_dir=backup_dir,
                )
            finally:
                fcntl.flock(lock_handle, fcntl.LOCK_UN)
    except OSError as exc:
        raise MigrationError(f"cannot lock database migration {lock_path}: {exc}") from exc


def _migrate_legacy_locked(
    *, db_path: str, config_path: str, bandwidth_path: str | None,
    snapshots_path: str | None, backup_dir: str | None,
) -> MigrationReport:
    if _legacy_migration_complete(db_path):
        existing = _json(config_path)
        if isinstance(existing, Mapping):
            _remove_legacy_dynamic_keys(config_path, cast(LegacyObject, existing))
        return MigrationReport(0, 0, 0, 0, 0, (), already_migrated=True)
    config = _legacy_object(_json(config_path), "config")
    bandwidth: LegacyObject = (
        _legacy_object(_json(bandwidth_path), "bandwidth history")
        if bandwidth_path else cast(LegacyObject, {})
    )
    snapshots: LegacyObject = (
        _legacy_object(_json(snapshots_path), "state snapshots")
        if snapshots_path else cast(LegacyObject, {})
    )

    users = _legacy_map(config, "users")
    tokens = _legacy_map(config, "tokens")
    fingerprints = _legacy_map(config, "userFingerprints")
    displaynames = _legacy_map(config, "displaynames")
    status = _legacy_map(config, "status")
    status_time = _legacy_map(config, "statusTime")
    status_wl = _legacy_map(config, "statusWl")
    times = _legacy_map(config, "time")
    bw_limits = _legacy_map(config, "bw")
    wl_limits = _legacy_map(config, "wl_bw")
    ext_users = _legacy_map(config, "webui_users")
    ext_passwords = _legacy_map(config, "webui_passwords")

    legacy_usernames = set(users)
    if len(legacy_usernames) != len(users):
        raise MigrationError("legacy user names are not unique")

    # validate credential maps once and resolve the one external login that
    # can be represented by each SQLite user row.
    # unknown references are retained in the source backup but skipped from the relational tables
    skipped_orphans = 0
    ext_for_user: dict[str, str] = {}
    for external, raw_username in ext_users.items():
        if not external:
            raise MigrationError("webui_users contains an empty external username")
        mapped_username = _legacy_string(raw_username, f"webui_users[{external!r}]")
        if mapped_username not in legacy_usernames:
            skipped_orphans += 1
            continue
        if mapped_username in ext_for_user:
            # the legacy map could contain stale aliases for one account
            # while SQLite intentionally keeps one credential per user
            skipped_orphans += 1
            continue
        ext_for_user[mapped_username] = external
    for external, raw_password in ext_passwords.items():
        _legacy_string(raw_password, f"webui_passwords[{external!r}]")

    prepared_users: list[LegacyUserRow] = []
    uuid_pattern = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    for name, raw_uuid in users.items():
        if not name:
            raise MigrationError("legacy username must not be empty")
        user_uuid = _legacy_string(raw_uuid, f"users[{name!r}]")
        if uuid_pattern.fullmatch(user_uuid) is None:
            raise MigrationError(f"users[{name!r}] is not a lowercase UUIDv4")
        token = _legacy_map_string(tokens, name, f"tokens[{name!r}]")
        if len(token) < 30:
            raise MigrationError(f"tokens[{name!r}] must contain at least 30 characters")
        fingerprint = _legacy_map_string(fingerprints, name, f"userFingerprints[{name!r}]")
        displayname = _legacy_map_string(displaynames, name, f"displaynames[{name!r}]", default=name)
        enabled = int(_legacy_map_bool(status, name, f"status[{name!r}]", default=True))
        enabled_time = int(_legacy_map_bool(status_time, name, f"statusTime[{name!r}]", default=True))
        enabled_wl = int(_legacy_map_bool(status_wl, name, f"statusWl[{name!r}]", default=True))
        expires_at = _legacy_map_int(times, name, f"time[{name!r}]", default=0, minimum=0)
        bw_limit, bw_used = _legacy_quota(bw_limits.get(name, (0, 0)), f"bw[{name!r}]")
        wl_limit, wl_used = _legacy_quota(wl_limits.get(name, (0, 0)), f"wl_bw[{name!r}]")
        external_for_user = ext_for_user.get(name)
        password = ext_passwords.get(external_for_user) if external_for_user is not None else None
        password_hash = _legacy_string(password, f"webui_passwords[{external_for_user!r}]") if password is not None else None
        prepared_users.append((
            name, user_uuid, token, fingerprint, displayname, enabled,
            enabled_time, enabled_wl, expires_at, bw_limit, bw_used,
            wl_limit, wl_used, external_for_user, password_hash,
        ))

    telegrams = _legacy_map(config, "tgids")
    valid_telegrams: dict[str, str] = {}
    for raw_telegram_id, raw_username in telegrams.items():
        telegram_id = _legacy_string(raw_telegram_id, "tgids key")
        username = _legacy_string(raw_username, f"tgids[{telegram_id!r}]")
        if username not in legacy_usernames:
            skipped_orphans += 1
            continue
        valid_telegrams[username] = telegram_id

    publicbot = _legacy_map(config, "publicbot")
    languages: LegacyObject = (
        _legacy_map(publicbot, "tg_lang")
        if "tg_lang" in publicbot else cast(LegacyObject, {})
    )
    for telegram_id, language in languages.items():
        _legacy_string(telegram_id, "publicbot.tg_lang key")
        language_value = _legacy_string(language, f"publicbot.tg_lang[{telegram_id!r}]")
        if language_value not in ("ru", "en"):
            raise MigrationError(f"invalid Telegram language for {telegram_id!r}")

    raw_codes = config.get("codes", [])
    if not isinstance(raw_codes, list):
        raise MigrationError("config.codes must be an array")
    prepared_codes: list[tuple[str, str, int, int, int, int, int]] = []
    for index, raw_item in enumerate(cast(list[object], raw_codes)):
        item = _legacy_object(raw_item, f"config.codes[{index}]")
        code = _legacy_map_string(item, "code", f"config.codes[{index}].code")
        if not code:
            raise MigrationError(f"config.codes[{index}].code must not be empty")
        action = _legacy_map_string(item, "action", f"config.codes[{index}].action")
        if action not in ("register", "bonus"):
            raise MigrationError(f"config.codes[{index}].action is invalid")
        permanent = int(_legacy_map_bool(item, "perma", f"config.codes[{index}].perma", default=False))
        uses = _legacy_map_int(item, "uses", f"config.codes[{index}].uses", default=1, minimum=0)
        days = _legacy_map_int(item, "days", f"config.codes[{index}].days", default=0, minimum=0)
        gb = _legacy_map_int(item, "gb", f"config.codes[{index}].gb", default=0, minimum=0)
        wl_gb = _legacy_map_int(item, "wl_gb", f"config.codes[{index}].wl_gb", default=0, minimum=0)
        prepared_codes.append((code, action, permanent, -1 if permanent else uses, days, gb, wl_gb))

    metadata: list[tuple[str, str]] = []
    last_reset = config.get("_last_reset")
    if last_reset is not None:
        value = _legacy_string(last_reset, "_last_reset")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
            raise MigrationError("_last_reset must be YYYY-MM-DD")
        metadata.append(("last_reset", value))
    last_reset_month = config.get("_last_reset_month")
    if last_reset_month is not None:
        value = _legacy_string(last_reset_month, "_last_reset_month")
        if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value) is None:
            raise MigrationError("_last_reset_month must be YYYY-MM")
        metadata.append(("last_reset_month", value))

    notification_values: list[tuple[str, list[str]]] = []
    for kind, key in (("regular", "_notified"), ("whitelist", "_wl_notified")):
        raw_values = config.get(key, [])
        if not isinstance(raw_values, list):
            raise MigrationError(f"config.{key} must be an array")
        ids: list[str] = []
        for item_value in cast(list[object], raw_values):
            if item_value is None:
                continue
            if not isinstance(item_value, int) or isinstance(item_value, bool):
                raise MigrationError(f"config.{key} contains a non-integer Telegram ID")
            ids.append(str(item_value))
        notification_values.append((kind, ids))

    raw_bandwidth_users = _legacy_map(bandwidth, "users")
    bandwidth_meta = _legacy_map(bandwidth, "_meta")
    bandwidth_retention_value = bandwidth_meta.get("retention_days")
    bandwidth_retention = (
        _legacy_int(bandwidth_retention_value, "bw_history._meta.retention_days", minimum=1)
        if bandwidth_retention_value is not None else None
    )
    bandwidth_by_key: dict[tuple[str, int], tuple[str, int, int, int, int, int]] = {}
    for raw_username, raw_payload in raw_bandwidth_users.items():
        username = _legacy_string(raw_username, "bw_history.users key")
        if username not in legacy_usernames:
            skipped_orphans += 1
            continue
        payload = _legacy_object(raw_payload, f"bw_history.users[{username!r}]")
        raw_snapshots = payload.get("snapshots", [])
        if not isinstance(raw_snapshots, list):
            raise MigrationError(f"bw_history.users[{username!r}].snapshots must be an array")
        for index, raw_snapshot in enumerate(cast(list[object], raw_snapshots)):
            snapshot = _legacy_object(raw_snapshot, f"bw_history.users[{username!r}].snapshots[{index}]")
            ts = _legacy_int(snapshot.get("ts"), "bandwidth snapshot ts")
            # These are daily deltas.  Existing installations can contain
            # negative values when a panel counter reset between snapshots;
            # preserve those signed values exactly during migration.
            up = _legacy_int(snapshot.get("up"), "bandwidth snapshot up")
            down = _legacy_int(snapshot.get("down"), "bandwidth snapshot down")
            wl_up = _legacy_int(snapshot.get("wl_up", 0), "bandwidth snapshot wl_up")
            wl_down = _legacy_int(snapshot.get("wl_down", 0), "bandwidth snapshot wl_down")
            snapshot_key = (username, ts)
            bandwidth_by_key[snapshot_key] = (username, ts, up, down, wl_up, wl_down)
    prepared_bandwidth = list(bandwidth_by_key.values())

    raw_state_rows: object = snapshots.get("snapshots", [])
    if not isinstance(raw_state_rows, list):
        raise MigrationError("snaps.json.snapshots must be an array")
    state_meta = _legacy_map(snapshots, "_meta")
    state_retention_value = state_meta.get("retention_days")
    state_retention = (
        _legacy_int(state_retention_value, "snaps._meta.retention_days", minimum=1)
        if state_retention_value is not None else None
    )
    state_by_timestamp: dict[int, str] = {}
    for index, raw_snapshot in enumerate(cast(list[object], raw_state_rows)):
        snapshot = _legacy_object(raw_snapshot, f"snaps.json.snapshots[{index}]")
        ts = _legacy_int(snapshot.get("ts"), "state snapshot ts")
        _legacy_object(snapshot.get("host"), f"state snapshot {ts}.host")
        _legacy_object(snapshot.get("panels"), f"state snapshot {ts}.panels")
        serialized = json.dumps(snapshot, separators=(",", ":"))
        state_by_timestamp[ts] = serialized
    prepared_state = list(state_by_timestamp.items())
    backup_root = backup_dir or os.path.join(os.path.dirname(os.path.abspath(db_path)), "migration-backups")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_paths = tuple(path for path in (
        _backup(config_path, backup_root, stamp),
        _backup(bandwidth_path, backup_root, stamp) if bandwidth_path else None,
        _backup(snapshots_path, backup_root, stamp) if snapshots_path else None,
    ) if path is not None)

    database: Database | None = None
    imported_users = imported_codes = imported_tgids = 0
    try:
        database = Database(db_path)
        with database.transaction(immediate=True) as conn:
            now = int(time.time())
            for row in prepared_users:
                conn.execute("""INSERT INTO users
                    (username, uuid, token, fingerprint, displayname, enabled, enabled_time,
                     enabled_wl, expires_at, bw_limit_gb, bw_used, wl_limit_gb, wl_used,
                     ext_username, ext_password_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*row, now))
                imported_users += 1
            for username, telegram_id in valid_telegrams.items():
                conn.execute(
                    "INSERT INTO telegram_mappings(telegram_id, username) VALUES (?, ?)",
                    (telegram_id, username),
                )
                imported_tgids += 1
            for telegram_id, language in languages.items():
                conn.execute(
                    "INSERT INTO telegram_preferences(telegram_id, language) VALUES (?, ?)",
                    (str(telegram_id), str(language)),
                )
            for code, action, permanent, uses, days, gb, wl_gb in prepared_codes:
                conn.execute(
                    "INSERT INTO codes(code, action, permanent, uses, days, gb, wl_gb, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (code, action, permanent, uses, days, gb, wl_gb, now),
                )
                imported_codes += 1
            for key, value in metadata:
                conn.execute(
                    "INSERT OR REPLACE INTO app_metadata(key, value) VALUES (?, ?)",
                    (key, value),
                )
            if bandwidth_retention is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO app_metadata(key, value) VALUES ('bw_retention_days', ?)",
                    (str(bandwidth_retention),),
                )
            if state_retention is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO app_metadata(key, value) VALUES ('state_retention_days', ?)",
                    (str(state_retention),),
                )
            for kind, ids in notification_values:
                for telegram_id in ids:
                    conn.execute(
                        "INSERT OR IGNORE INTO notification_state(kind, telegram_id) VALUES (?, ?)",
                        (kind, telegram_id),
                    )
            for username, ts, up, down, wl_up, wl_down in prepared_bandwidth:
                conn.execute(
                    """INSERT INTO bandwidth_snapshots(username, ts, up, down, wl_up, wl_down)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(username, ts) DO UPDATE SET
                           up=excluded.up, down=excluded.down,
                           wl_up=excluded.wl_up, wl_down=excluded.wl_down""",
                    (username, ts, up, down, wl_up, wl_down),
                )
            for ts, serialized in prepared_state:
                conn.execute(
                    "INSERT INTO state_snapshots(ts, payload) VALUES (?, ?) ON CONFLICT(ts) DO UPDATE SET payload=excluded.payload",
                    (ts, serialized),
                )
            actual_users = int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
            actual_codes = int(conn.execute("SELECT COUNT(*) FROM codes").fetchone()[0])
            actual_tgids = int(conn.execute("SELECT COUNT(*) FROM telegram_mappings").fetchone()[0])
            if (actual_users, actual_codes, actual_tgids) != (imported_users, imported_codes, imported_tgids):
                raise MigrationError("imported row counts do not match validated legacy source")
            conn.execute("INSERT OR REPLACE INTO app_metadata(key, value) VALUES ('legacy_migration', 'complete')")
        _remove_legacy_dynamic_keys(config_path, config)
    except (MigrationError, DatabaseError):
        raise
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise MigrationError(f"legacy data could not be imported: {exc}") from exc
    finally:
        if database is not None:
            database.close()
    return MigrationReport(
        imported_users, imported_codes, imported_tgids,
        len(prepared_bandwidth), len(prepared_state), backup_paths,
        skipped_orphans,
    )
