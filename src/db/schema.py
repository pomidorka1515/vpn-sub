from __future__ import annotations

import sqlite3

from errors import MigrationError

from .common import ConnectionMixin


SCHEMA_STATEMENTS = (
    """CREATE TABLE users (
        username TEXT PRIMARY KEY,
        uuid TEXT NOT NULL UNIQUE,
        token TEXT NOT NULL UNIQUE,
        auth_token TEXT UNIQUE,
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
    "CREATE INDEX idx_users_auth_token ON users(auth_token)",
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


class SchemaMixin(ConnectionMixin):
    SCHEMA_VERSION = 3

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
                    for statement in SCHEMA_STATEMENTS:
                        conn.execute(statement)
                    version = 1
                if version == 1:
                    conn.execute(
                        """UPDATE bandwidth_snapshots
                        SET up = max(up, 0), down = max(down, 0),
                            wl_up = max(wl_up, 0), wl_down = max(wl_down, 0)"""
                    )
                if version == 2:
                    conn.execute("ALTER TABLE users ADD COLUMN auth_token TEXT")
                    conn.execute("CREATE UNIQUE INDEX idx_users_auth_token ON users(auth_token)")
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
