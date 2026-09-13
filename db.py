"""SQLite persistence for dynamic subscription state.

The JSON configuration contains deployment and presentation settings. This module owns mutable user, code, quota, Telegram, notification and snapshot state.
Connections are deliberately short lived: every operation obtains a connection for its transaction, which is safe for Flask and background threads.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import glob
import json
import os
import sqlite3
import tempfile
import threading
import time
from collections.abc import Generator, Mapping
from typing import Any, cast, TypedDict
from pathlib import Path

from loggers import Logger
from errors import DatabaseError, CodeError, MigrationError, DuplicateError
__all__ = [
    "Database", "DatabaseError", "DuplicateError", "CodeError",
    "MigrationError", "MigrationReport", "UserRecord", "CodeRecord",
    "RegisterCodeResult", "BonusResult"
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


def _instance_backup_dir(path: str, backup_dir: str) -> str:
    name = os.path.splitext(os.path.basename(path))[0]
    return os.path.join(backup_dir, name)


def _do_backup(path: str, timeout: float, instance_dir: str, log: Logger) -> None:
    """Take an atomic SQLite snapshot in a per-instance backup directory."""
    os.makedirs(instance_dir, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = os.path.join(instance_dir, f"{timestamp}.sqlite3")
    fd, temporary = tempfile.mkstemp(dir=instance_dir, prefix=".tmp-", suffix=".sqlite3")
    os.close(fd)

    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    try:
        source = sqlite3.connect(path, timeout=timeout)
        destination = sqlite3.connect(temporary)
        source.backup(destination)
        destination.commit()
        destination.close()
        destination = None
        source.close()
        source = None
        os.replace(temporary, backup_path)
    except Exception:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise

    log.debug(f"backup saved: {backup_path}")


def _prune_backups(instance_dir: str, retention: int, log: Logger) -> None:
    """Keep only the N most recent database backups."""
    files = sorted(glob.glob(os.path.join(instance_dir, "*.sqlite3")))
    to_delete = files[:-retention]
    for path in to_delete:
        try:
            os.unlink(path)
            log.debug(f"pruned old backup: {path}")
        except OSError as exc:
            log.error(f"prune failed for {path}: {exc}")


def _make_backup_thread(
    *,
    path: str,
    timeout: float,
    backup_dir: str,
    backup_interval: int | float,
    backup_retention: int,
    stop_event: threading.Event,
) -> threading.Thread:
    def loop() -> None:
        log = Logger("Backup")
        instance_dir = _instance_backup_dir(path, backup_dir)
        while not stop_event.wait(backup_interval):
            try:
                _do_backup(path, timeout, instance_dir, log)
                _prune_backups(instance_dir, backup_retention, log)
                log.info("backup successful")
            except Exception as exc:
                log.error(f"backup failed: {exc}")

    return threading.Thread(target=loop, daemon=True, name="Backup")



class Database:
    """Thread-safe SQLite repository for dynamic application state."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        *,
        path: str | Path,
        timeout: float = 5.0,
        backup_dir: str | Path | None = None,
        backup_interval: int | float = 7200,
        backup_retention: int = 3,
    ) -> None:
        """
        Args:
            path: Path to the SQLite database.
            timeout: Connection timeout in seconds.
            backup_dir: Backup directory. Backups are disabled if set to None.
            backup_interval: Interval in seconds for the backups.
            backup_retention: Amount of concurrent backups kept on disk.
        """
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.path = os.path.abspath(path)
            self.timeout = timeout
            self._connections: set[sqlite3.Connection] = set()
            self._connections_lock = threading.Lock()
            self._backup_dir: str | None = str(backup_dir) if backup_dir else None
            self._backup_interval: int | float = backup_interval
            self._backup_retention: int = backup_retention
            self._backup_stop = threading.Event()
            self._backup_t: threading.Thread | None = None
            parent = os.path.dirname(self.path)
            try:
                if parent:
                    os.makedirs(parent, exist_ok=True)
            except OSError as exc:
                raise DatabaseError(f"unable to create database directory {parent}: {exc}") from exc
            self.initialize()
            if self._backup_dir:
                self._backup_t = _make_backup_thread(
                    path=self.path,
                    timeout=self.timeout,
                    backup_dir=self._backup_dir,
                    backup_interval=self._backup_interval,
                    backup_retention=self._backup_retention,
                    stop_event=self._backup_stop,
                )
                self._backup_t.start()

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

    @property
    def backup_dir(self) -> str | None:
        return self._backup_dir

    @property
    def backup_interval(self) -> int | float:
        return self._backup_interval

    @property
    def backup_retention(self) -> int:
        return self._backup_retention

    def backup_now(self) -> None:
        """Trigger an immediate backup snapshot, regardless of the schedule.

        Obeys the same retention limit as the scheduled backup thread.

        Raises:
            DatabaseError: If no backup_dir was configured on this instance.
        """
        if self._backup_dir is None:
            raise DatabaseError("backup_now() requires a backup_dir to be configured.")
        instance_dir = _instance_backup_dir(self.path, self._backup_dir)
        _do_backup(self.path, self.timeout, instance_dir, self.log)
        _prune_backups(instance_dir, self._backup_retention, self.log)

    def close(self) -> None:
        self._backup_stop.set()
        if self._backup_t is not None:
            self._backup_t.join(timeout=5)
        with self._connections_lock:
            connections = tuple(self._connections)
            self._connections.clear()
        for conn in connections:
            conn.close()
