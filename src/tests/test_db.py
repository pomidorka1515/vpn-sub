from __future__ import annotations

from pathlib import Path
import sqlite3
import threading
import uuid

import pytest

from errors import CodeError, DuplicateError, MigrationError
from db import Database


def create_user(db: Database, username: str = "alice", *, token: str | None = None) -> None:
    db.create_user(
        username=username,
        uuid=str(uuid.uuid4()),
        token=token or f"{username}-{'x' * 32}",
        fingerprint="chrome",
        displayname=username.title(),
        ext_username=f"{username}@example.test",
        ext_password_hash="hash",
    )


def test_schema_initialization_is_idempotent(db_path: Path) -> None:
    first = Database(path=db_path)
    first.close()
    db = Database(path=db_path)
    try:
        assert db.list_users() == []
        assert db.get_metadata("missing") is None
    finally:
        db.close()


def test_unversioned_existing_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "unversioned.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE users(username TEXT PRIMARY KEY)")
    connection.close()
    with pytest.raises(MigrationError):
        Database(path=path)


def test_migration_repairs_negative_bandwidth_snapshot_values(
    database: Database, db_path: Path,
) -> None:
    database.close()
    connection = sqlite3.connect(db_path)
    connection.execute("UPDATE schema_version SET version = 1")
    connection.execute(
        """INSERT INTO bandwidth_snapshots(username, ts, up, down, wl_up, wl_down)
        VALUES ('alice', 20, -1, 2, -3, 4)"""
    )
    connection.execute(
        """INSERT INTO bandwidth_snapshots(username, ts, up, down, wl_up, wl_down)
        VALUES ('alice', 30, 5, -6, 7, 8)"""
    )
    connection.commit()
    connection.close()

    db = Database(path=db_path)
    try:
        rows = db.get_bandwidth_snapshots("alice", 0)
        assert [row["up"] for row in rows] == [5, 0]
        assert [row["down"] for row in rows] == [0, 2]
        assert [row["wl_up"] for row in rows] == [7, 0]
        assert [row["wl_down"] for row in rows] == [8, 4]
    finally:
        db.close()


def test_auth_token_migration_and_lookup(database: Database, db_path: Path) -> None:
    database.close()
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
        UPDATE schema_version SET version = 2;"""
    )
    connection.close()

    db = Database(path=db_path)
    try:
        create_user(db)
        record = db.get_user("alice")
        assert record is not None
        assert record["auth_token"] is None
        db.set_auth_token("alice", "a" * 100)
        assert db.auth_token_to_user("a" * 100) == "alice"
        assert db.auth_token_to_user("b" * 100) is None
        db.set_auth_token("alice", None)
        assert db.auth_token_to_user("a" * 100) is None
    finally:
        db.close()


def test_user_constraints_reverse_lookups_and_deletion(database: Database) -> None:
    token = "t" * 40
    create_user(database, token=token)
    database.set_telegram("alice", "123")
    assert database.token_to_user(token) == "alice"
    assert database.ext_to_user("alice@example.test") == "alice"
    assert database.tgid_to_user(123) == "alice"
    with pytest.raises(DuplicateError):
        create_user(database, "bob", token=token)
    database.delete_user("alice")
    assert database.tgid_to_user(123) is None
    assert database.ext_to_user("alice@example.test") is None


def test_registration_and_bonus_consumption_are_atomic(database: Database) -> None:
    database.add_code("invite", "register", uses=1, days=3, gb=2, wl_gb=1)
    successes: list[str] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def register(index: int) -> None:
        try:
            database.register_with_code(
                code="invite", username=f"u{index}", uuid=str(uuid.uuid4()),
                token=f"{index}{'x' * 39}", fingerprint="chrome",
                displayname=f"U {index}", ext_username=f"u{index}@example.test",
                ext_password_hash="hash",
            )
            with lock:
                successes.append(str(index))
        except CodeError as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=register, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(successes) == 1
    assert len(errors) == 7
    assert database.get_code("invite") is None

    username = f"u{successes[0]}"
    database.add_code("bonus", "bonus", uses=2, days=1, gb=3, wl_gb=4)
    result = database.adjust_bonus(username, "bonus")
    assert result["uses"] == 1
    user = database.get_user(username)
    assert user is not None
    assert user["bw_limit_gb"] == 5
    assert user["wl_limit_gb"] == 5


def test_metadata_prefix_listing_and_deletion(database: Database) -> None:
    database.set_metadata("prefix_one", "1")
    database.set_metadata("prefix_two", "2")
    database.set_metadata("prefix_100%", "3")
    database.set_metadata("other", "4")
    assert database.list_metadata("prefix_") == {
        "prefix_one": "1", "prefix_two": "2", "prefix_100%": "3",
    }
    database.delete_metadata("prefix_one")
    assert database.get_metadata("prefix_one") is None
    assert database.get_metadata("prefix_two") == "2"


def test_registration_sync_rollback_refunds_finite_code(database: Database) -> None:
    database.add_code("invite", "register", uses=1)
    database.register_with_code(
        code="invite", username="alice", uuid=str(uuid.uuid4()),
        token="a" * 40, fingerprint="chrome", displayname="Alice",
        ext_username="alice@example.test", ext_password_hash="hash",
    )
    assert database.get_code("invite") is None
    database.rollback_registration_sync("alice")
    assert not database.user_exists("alice")
    restored = database.get_code("invite")
    assert restored is not None
    assert restored["uses"] == 1


def test_notifications_monthly_reset_and_snapshots(database: Database) -> None:
    create_user(database)
    assert database.mark_notification("regular", 123)
    assert not database.mark_notification("regular", 123)
    database.increment_usage("alice", regular=1234, whitelist=2345)
    assert database.reset_monthly("2026-09", "2026-09-01")
    assert not database.reset_monthly("2026-09", "2026-09-01")
    user = database.get_user("alice")
    assert user is not None
    assert (user["bw_used"], user["wl_used"]) == (0, 0)
    assert not database.notification_seen("regular", 123)

    database.add_bandwidth_snapshot("alice", 10, 1, 2, 3, 4)
    database.add_bandwidth_snapshot("alice", 10, 5, 6, 7, 8)
    database.add_bandwidth_snapshot("alice", 20, 9, 10, 11, 12)
    bandwidth_rows = database.get_bandwidth_snapshots("alice", 0)
    assert [row["ts"] for row in bandwidth_rows] == [20, 10]
    assert bandwidth_rows[1]["up"] == 6
    assert database.prune_bandwidth_snapshots(20) == 1
    database.upsert_state_snapshot(10, {"ts": 10, "host": {}, "panels": {}})
    database.upsert_state_snapshot(10, {"ts": 10, "host": {"new": True}, "panels": {}})
    assert database.get_state_snapshots(0)[0]["host"] == {"new": True}
    assert database.prune_state_snapshots(20) == 1
