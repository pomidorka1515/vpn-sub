from __future__ import annotations

import sqlite3
import time
import uuid
from typing import TYPE_CHECKING

import pytest

from constants import SESSION_MAX_AGE
from db import Database
from errors import DuplicateError, MigrationError

if TYPE_CHECKING:
    from pathlib import Path


async def create_user(db: Database, username: str = "alice", *, token: str | None = None) -> None:
    (
        await db.create_user(
            username=username,
            uuid=str(uuid.uuid4()),
            token=token or f"{username}-{'x' * 32}",
            fingerprint="chrome",
            displayname=username.title(),
            ext_username=f"{username}@example.test",
            ext_password_hash="hash",
        )
    )


@pytest.mark.asyncio
async def test_schema_initialization_is_idempotent(db_path: Path) -> None:
    first = Database(path=db_path)
    await first.start()
    (await first.close())
    db = Database(path=db_path)
    await db.start()
    try:
        assert (await db.list_users()) == []
        assert (await db.get_metadata("missing")) is None
        assert (await db.admin_ui_session()) is None
        with sqlite3.connect(db_path) as conn:
            assert (
                conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()[0] == 7
            )
            assert (
                conn.execute(
                    "SELECT value FROM app_metadata WHERE key = 'admin_ui_session'",
                ).fetchone()[0]
                == ""
            )
        (await db.set_admin_ui_session("a" * 100))
        assert (await db.admin_ui_session()) == "a" * 100
        (await db.set_admin_ui_session(None))
        assert (await db.admin_ui_session()) is None
    finally:
        (await db.close())


@pytest.mark.asyncio
async def test_sessions_expire_and_stay_expired_after_restart(
    database: Database,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    await create_user(database)
    await database.set_auth_token("alice", "web-session")
    await database.set_admin_ui_session("admin-session")
    now += SESSION_MAX_AGE - 1
    assert await database.auth_token_to_user("web-session") == "alice"
    assert await database.admin_ui_session() == "admin-session"
    now += 1
    assert await database.auth_token_to_user("web-session") is None
    assert await database.admin_ui_session() is None
    await database.close()
    db = Database(path=db_path)
    await db.start()
    try:
        assert await db.auth_token_to_user("web-session") is None
        assert await db.admin_ui_session() is None
        await db.set_auth_token("alice", "new-web-session")
        await db.set_admin_ui_session("new-admin-session")
        assert await db.auth_token_to_user("new-web-session") == "alice"
        assert await db.admin_ui_session() == "new-admin-session"
        await db.set_auth_token("alice", None)
        record = await db.get_user("alice")
        assert record is not None
        assert record["auth_token_expires_at"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_session_expiry_migration_revokes_legacy_sessions(
    database: Database,
    db_path: Path,
) -> None:
    await create_user(database)
    await database.set_auth_token("alice", "legacy-web")
    await database.set_admin_ui_session("legacy-admin")
    await database.close()
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE users DROP COLUMN auth_token_expires_at")
        conn.execute("ALTER TABLE users DROP COLUMN admin_disabled")
        conn.execute("ALTER TABLE users DROP COLUMN admin_disabled_wl")
        conn.execute("DELETE FROM app_metadata WHERE key = 'admin_ui_session_expires_at'")
        conn.execute("ALTER TABLE bandwidth_snapshots DROP COLUMN start_ts")
        conn.execute("UPDATE schema_version SET version = 4")
    db = Database(path=db_path)
    await db.start()
    try:
        assert await db.auth_token_to_user("legacy-web") is None
        assert await db.admin_ui_session() is None
        record = await db.get_user("alice")
        assert record is not None
        assert record["auth_token"] is None
        assert record["auth_token_expires_at"] is None
        assert await db.token_to_user(record["token"]) == "alice"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unversioned_existing_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "unversioned.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE users(username TEXT PRIMARY KEY)")
    connection.close()
    with pytest.raises(MigrationError):
        await Database(path=path).start()


@pytest.mark.asyncio
async def test_migration_repairs_negative_bandwidth_snapshot_values(
    database: Database,
    db_path: Path,
) -> None:
    (await database.close())
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """ALTER TABLE users RENAME TO old_users;
        CREATE TABLE users (
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
        );
        INSERT INTO users SELECT username, uuid, token, fingerprint, displayname,
            enabled, enabled_time, enabled_wl, expires_at, bw_limit_gb, bw_used,
            wl_limit_gb, wl_used, ext_username, ext_password_hash, created_at
        FROM old_users;
        DROP TABLE old_users;
        ALTER TABLE bandwidth_snapshots DROP COLUMN start_ts;
        UPDATE schema_version SET version = 1;""",
    )
    connection.execute(
        """INSERT INTO bandwidth_snapshots(username, ts, up, down, wl_up, wl_down)
        VALUES ('alice', 20, -1, 2, -3, 4)""",
    )
    connection.execute(
        """INSERT INTO bandwidth_snapshots(username, ts, up, down, wl_up, wl_down)
        VALUES ('alice', 30, 5, -6, 7, 8)""",
    )
    connection.commit()
    connection.close()

    db = Database(path=db_path)
    await db.start()
    try:
        rows = await db.get_bandwidth_snapshots("alice", 0)
        assert [row["up"] for row in rows] == [5, 0]
        assert [row["down"] for row in rows] == [0, 2]
        assert [row["wl_up"] for row in rows] == [7, 0]
        assert [row["wl_down"] for row in rows] == [8, 4]
    finally:
        (await db.close())


@pytest.mark.asyncio
async def test_auth_token_migration_and_lookup(database: Database, db_path: Path) -> None:
    (await database.close())
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """ALTER TABLE users RENAME TO old_users;
        CREATE TABLE users (
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
        );
        INSERT INTO users SELECT username, uuid, token, fingerprint, displayname,
            enabled, enabled_time, enabled_wl, expires_at, bw_limit_gb, bw_used,
            wl_limit_gb, wl_used, ext_username, ext_password_hash, created_at
        FROM old_users;
        DROP TABLE old_users;
        ALTER TABLE bandwidth_snapshots DROP COLUMN start_ts;
        UPDATE schema_version SET version = 2;""",
    )
    connection.close()

    db = Database(path=db_path)
    await db.start()
    try:
        (await create_user(db))
        record = await db.get_user("alice")
        assert record is not None
        assert record["auth_token"] is None
        (await db.set_auth_token("alice", "a" * 100))
        assert (await db.auth_token_to_user("a" * 100)) == "alice"
        assert (await db.auth_token_to_user("b" * 100)) is None
        (await db.set_auth_token("alice", None))
        assert (await db.auth_token_to_user("a" * 100)) is None
        assert (await db.admin_ui_session()) is None
    finally:
        (await db.close())


@pytest.mark.asyncio
async def test_admin_ui_session_migration_from_version_3(database: Database, db_path: Path) -> None:
    (await database.close())
    connection = sqlite3.connect(db_path)
    connection.execute("DELETE FROM app_metadata WHERE key = 'admin_ui_session'")
    connection.execute("ALTER TABLE users DROP COLUMN auth_token_expires_at")
    connection.execute("ALTER TABLE users DROP COLUMN admin_disabled")
    connection.execute("ALTER TABLE users DROP COLUMN admin_disabled_wl")
    connection.execute("ALTER TABLE bandwidth_snapshots DROP COLUMN start_ts")
    connection.execute("UPDATE schema_version SET version = 3")
    connection.commit()
    connection.close()

    db = Database(path=db_path)
    await db.start()
    try:
        assert (await db.admin_ui_session()) is None
        with sqlite3.connect(db_path) as conn:
            assert (
                conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()[0] == 7
            )
            assert (
                conn.execute(
                    "SELECT value FROM app_metadata WHERE key = 'admin_ui_session'",
                ).fetchone()[0]
                == ""
            )
        (await db.set_admin_ui_session("c" * 100))
        assert (await db.admin_ui_session()) == "c" * 100
    finally:
        (await db.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("enabled_wl", [False, True])
async def test_disable_intent_migration_preserves_legacy_flags(
    database: Database,
    db_path: Path,
    enabled: bool,
    enabled_wl: bool,
) -> None:
    await create_user(database)
    await database.set_user("alice", status=enabled, status_wl=enabled_wl)
    await database.close()
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE users DROP COLUMN admin_disabled")
        conn.execute("ALTER TABLE users DROP COLUMN admin_disabled_wl")
        conn.execute("ALTER TABLE bandwidth_snapshots DROP COLUMN start_ts")
        conn.execute("UPDATE schema_version SET version = 5")
    for _ in range(2):
        db = Database(path=db_path, start_backup=False)
        await db.start()
        try:
            state = await db.get_user("alice")
            assert state is not None
            assert bool(state["admin_disabled"]) is not enabled
            assert bool(state["admin_disabled_wl"]) is not enabled_wl
            # Intent and effective flags commit together and survive restart.
            await db.set_enabled_pending(
                "alice",
                enabled=True,
                enabled_time=True,
                enabled_wl=True,
                quota=True,
            )
            state = await db.get_user("alice")
            assert state is not None
            assert bool(state["enabled"]) is enabled
            assert bool(state["enabled_wl"]) is enabled_wl
        finally:
            await db.close()


@pytest.mark.asyncio
async def test_user_constraints_reverse_lookups_and_deletion(database: Database) -> None:
    token = "t" * 40
    (await create_user(database, token=token))
    (await database.set_telegram("alice", "123"))
    assert (await database.token_to_user(token)) == "alice"
    assert (await database.ext_to_user("alice@example.test")) == "alice"
    assert (await database.tgid_to_user(123)) == "alice"
    with pytest.raises(DuplicateError):
        (await create_user(database, "bob", token=token))
    (await database.delete_user("alice"))
    assert (await database.tgid_to_user(123)) is None
    assert (await database.ext_to_user("alice@example.test")) is None


@pytest.mark.asyncio
async def test_metadata_prefix_listing_and_deletion(database: Database) -> None:
    (await database.set_metadata("prefix_one", "1"))
    (await database.set_metadata("prefix_two", "2"))
    (await database.set_metadata("prefix_100%", "3"))
    (await database.set_metadata("other", "4"))
    assert (await database.list_metadata("prefix_")) == {
        "prefix_one": "1",
        "prefix_two": "2",
        "prefix_100%": "3",
    }
    (await database.delete_metadata("prefix_one"))
    assert (await database.get_metadata("prefix_one")) is None
    assert (await database.get_metadata("prefix_two")) == "2"


@pytest.mark.asyncio
async def test_registration_sync_rollback_refunds_finite_code(database: Database) -> None:
    (await database.add_code("invite", "register", uses=1))
    (
        await database.register_with_code(
            code="invite",
            username="alice",
            uuid=str(uuid.uuid4()),
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            ext_username="alice@example.test",
            ext_password_hash="hash",
        )
    )
    assert (await database.get_code("invite")) is None
    (await database.rollback_registration_sync("alice"))
    assert not (await database.user_exists("alice"))
    restored = await database.get_code("invite")
    assert restored is not None
    assert restored["uses"] == 1


@pytest.mark.asyncio
async def test_notifications_monthly_reset_and_snapshots(database: Database) -> None:
    (await create_user(database))
    assert await database.mark_notification("regular", 123)
    assert not (await database.mark_notification("regular", 123))
    (await database.mark_notification("alice:traffic_exhausted:1", "123"))
    (await database.mark_notification("alice:traffic_exhausted:1", ""))
    (
        await database.sync_notifications(
            {"alice:traffic_exhausted:1"},
            {"alice:restored"},
        )
    )
    assert (await database.notification_markers()) == {"alice:restored"}
    # a legacy row sharing the kind string must survive a marker delete
    assert await database.notification_seen("alice:traffic_exhausted:1", 123)
    assert not (await database.notification_seen("alice:traffic_exhausted:1", ""))
    (await database.increment_usage("alice", regular=1234, whitelist=2345))
    assert await database.reset_monthly("2026-09", "2026-09-01")
    assert not (await database.reset_monthly("2026-09", "2026-09-01"))
    user = await database.get_user("alice")
    assert user is not None
    assert (user["bw_used"], user["wl_used"]) == (0, 0)
    assert await database.notification_seen("regular", 123)
    assert await database.notification_markers() == {"alice:restored"}

    (await database.add_bandwidth_snapshot("alice", 10, 1, 2, 3, 4))
    (await database.add_bandwidth_snapshot("alice", 10, 5, 6, 7, 8))
    (await database.add_bandwidth_snapshot("alice", 20, 9, 10, 11, 12))
    bandwidth_rows = await database.get_bandwidth_snapshots("alice", 0)
    assert [row["ts"] for row in bandwidth_rows] == [20, 10]
    assert bandwidth_rows[1]["up"] == 6
    assert (await database.prune_bandwidth_snapshots(20)) == 1
    (await database.upsert_state_snapshot(10, {"ts": 10, "host": {}, "panels": {}}))
    (await database.upsert_state_snapshot(10, {"ts": 10, "host": {"new": True}, "panels": {}}))
    assert (await database.get_state_snapshots(0))[0]["host"] == {"new": True}
    assert (await database.prune_state_snapshots(20)) == 1


@pytest.mark.asyncio
async def test_bandwidth_snapshot_preserves_named_integer_fields(database: Database) -> None:
    (
        await database.create_user(
            username="snapshot",
            uuid="uuid-snapshot",
            token="token-snapshot",
            fingerprint="chrome",
            displayname="Snapshot",
        )
    )
    (await database.add_bandwidth_snapshot("snapshot", 100, 1, 2, 3, 4))
    assert (await database.get_bandwidth_snapshots("snapshot", 100)) == [
        {"ts": 100, "up": 1, "down": 2, "wl_up": 3, "wl_down": 4},
    ]
    assert (await database.get_bandwidth_snapshots("snapshot", 101)) == []


@pytest.mark.asyncio
async def test_bandwidth_interval_migration_and_end_filtered_history(
    database: Database,
    db_path: Path,
) -> None:
    await create_user(database)
    await database.add_bandwidth_snapshot("alice", 100, 1, 2, 3, 4)
    await database.close()
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE bandwidth_snapshots DROP COLUMN start_ts")
        conn.execute("UPDATE schema_version SET version = 6")
    db = Database(path=db_path, start_backup=False)
    await db.start()
    try:
        legacy = await db.get_bandwidth_snapshots("alice", 0)
        assert legacy[0].get("start_ts") is None
        await db.add_bandwidth_snapshots([("alice", 200, 10, 20, 30, 40)], starts={"alice": 50})
        # The interval crosses the cutoff: retain its true start, not the cutoff.
        rows = await db.get_bandwidth_snapshots("alice", 150)
        assert len(rows) == 1
        assert rows[0].get("start_ts") == 50
        await db.prune_bandwidth_snapshots(150)
        assert (await db.get_bandwidth_snapshots("alice", 0))[0].get("start_ts") == 50
        # A repeated same-second sample adds deltas but preserves the encompassing start.
        await db.add_bandwidth_snapshots([("alice", 200, 1, 2, 3, 4)], starts={"alice": 200})
        assert (await db.get_bandwidth_snapshots("alice", 0))[0].get("start_ts") == 50
        # A legacy/unknown contribution makes the merged start unknown again.
        await db.add_bandwidth_snapshot("alice", 200, 1, 2, 3, 4)
        assert (await db.get_bandwidth_snapshots("alice", 0))[0].get("start_ts") is None
    finally:
        await db.close()
