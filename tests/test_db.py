from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import uuid

from db import CodeError, Database, DatabaseError, DuplicateError, MigrationError, migrate_legacy


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.db = Database(str(self.path), timeout=2)

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def create_user(self, username: str = "alice", *, token: str | None = None) -> None:
        self.db.create_user(
            username=username,
            uuid=str(uuid.uuid4()),
            token=token or f"{username}-{'x' * 32}",
            fingerprint="chrome",
            displayname=username.title(),
            ext_username=f"{username}@example.test",
            ext_password_hash="hash",
        )

    def test_schema_initialization_is_idempotent(self) -> None:
        self.db.close()
        self.db = Database(str(self.path))
        self.assertEqual(self.db.list_users(), [])
        self.assertEqual(self.db.get_metadata("missing"), None)

    def test_unversioned_existing_database_is_refused(self) -> None:
        path = Path(self.temp.name) / "unversioned.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE users(username TEXT PRIMARY KEY)")
        connection.close()
        with self.assertRaises(MigrationError):
            Database(str(path))

    def test_user_constraints_reverse_lookups_and_deletion(self) -> None:
        token = "t" * 40
        self.create_user(token=token)
        self.db.set_telegram("alice", "123")
        self.assertEqual(self.db.token_to_user(token), "alice")
        self.assertEqual(self.db.ext_to_user("alice@example.test"), "alice")
        self.assertEqual(self.db.tgid_to_user(123), "alice")
        with self.assertRaises(DuplicateError):
            self.create_user("bob", token=token)
        self.db.delete_user("alice")
        self.assertIsNone(self.db.tgid_to_user(123))
        self.assertIsNone(self.db.ext_to_user("alice@example.test"))

    def test_registration_and_bonus_consumption_are_atomic(self) -> None:
        self.db.add_code("invite", "register", uses=1, days=3, gb=2, wl_gb=1)
        successes: list[str] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def register(index: int) -> None:
            try:
                self.db.register_with_code(
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
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(errors), 7)
        self.assertIsNone(self.db.get_code("invite"))

        username = f"u{successes[0]}"
        self.db.add_code("bonus", "bonus", uses=2, days=1, gb=3, wl_gb=4)
        result = self.db.adjust_bonus(username, "bonus")
        self.assertEqual(result["uses"], 1)
        user = self.db.get_user(username)
        assert user is not None
        self.assertEqual(user["bw_limit_gb"], 5)
        self.assertEqual(user["wl_limit_gb"], 5)

    def test_registration_sync_rollback_refunds_finite_code(self) -> None:
        self.db.add_code("invite", "register", uses=1)
        self.db.register_with_code(
            code="invite", username="alice", uuid=str(uuid.uuid4()),
            token="a" * 40, fingerprint="chrome", displayname="Alice",
            ext_username="alice@example.test", ext_password_hash="hash",
        )
        self.assertIsNone(self.db.get_code("invite"))
        self.db.rollback_registration_sync("alice")
        self.assertFalse(self.db.user_exists("alice"))
        restored = self.db.get_code("invite")
        assert restored is not None
        self.assertEqual(restored["uses"], 1)

    def test_notifications_monthly_reset_and_snapshots(self) -> None:
        self.create_user()
        self.assertTrue(self.db.mark_notification("regular", 123))
        self.assertFalse(self.db.mark_notification("regular", 123))
        self.db.increment_usage("alice", regular=1234, whitelist=2345)
        self.assertTrue(self.db.reset_monthly("2026-09", "2026-09-01"))
        self.assertFalse(self.db.reset_monthly("2026-09", "2026-09-01"))
        user = self.db.get_user("alice")
        assert user is not None
        self.assertEqual((user["bw_used"], user["wl_used"]), (0, 0))
        self.assertFalse(self.db.notification_seen("regular", 123))

        self.db.upsert_bandwidth_snapshot("alice", 10, 1, 2, 3, 4)
        self.db.upsert_bandwidth_snapshot("alice", 10, 5, 6, 7, 8)
        self.db.upsert_bandwidth_snapshot("alice", 20, 9, 10, 11, 12)
        bandwidth_rows = self.db.get_bandwidth_snapshots("alice", 0)
        self.assertEqual([row["ts"] for row in bandwidth_rows], [20, 10])
        self.assertEqual(bandwidth_rows[1]["up"], 5)
        self.assertEqual(self.db.prune_bandwidth_snapshots(20), 1)
        self.db.upsert_state_snapshot(10, {"ts": 10, "host": {}, "panels": {}})
        self.db.upsert_state_snapshot(10, {"ts": 10, "host": {"new": True}, "panels": {}})
        self.assertEqual(self.db.get_state_snapshots(0)[0]["host"], {"new": True})
        self.assertEqual(self.db.prune_state_snapshots(20), 1)


class MigrationTests(unittest.TestCase):
    def test_legacy_import_is_complete_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            bw_path = root / "bw.json"
            snapshots_path = root / "snapshots.json"
            db_path = root / "state.sqlite3"
            legacy = {
                "users": {"alice": "00000000-0000-4000-8000-000000000001"},
                "tokens": {"alice": "t" * 40},
                "userFingerprints": {"alice": "chrome"},
                "displaynames": {"alice": "Alice"},
                "status": {"alice": True}, "statusTime": {"alice": True},
                "statusWl": {"alice": False}, "time": {"alice": 100},
                "bw": {"alice": [10, 20]}, "wl_bw": {"alice": [5, 6]},
                "webui_users": {"alice@example.test": "alice"},
                "webui_passwords": {"alice@example.test": "hash"},
                "tgids": {"123": "alice"},
                "publicbot": {"tg_lang": {"123": "en"}},
                "codes": [{"code": "code", "action": "bonus", "perma": False, "uses": 2, "days": 1, "gb": 2, "wl_gb": 3}],
                "_notified": [123], "_wl_notified": [], "_last_reset_month": "2026-09",
            }
            config_path.write_text(json.dumps(legacy), encoding="utf-8")
            config_path.chmod(0o600)
            bw_path.write_text(json.dumps({"users": {"alice": {"snapshots": [{"ts": 10, "up": 1, "down": 2, "wl_up": 3, "wl_down": 4}]}}}), encoding="utf-8")
            snapshots_path.write_text(json.dumps({"snapshots": [{"ts": 10, "host": {}, "panels": {}}]}), encoding="utf-8")

            report = migrate_legacy(db_path=str(db_path), config_path=str(config_path), bandwidth_path=str(bw_path), snapshots_path=str(snapshots_path))
            self.assertEqual((report.users, report.codes, report.telegram_mappings), (1, 1, 1))
            self.assertEqual((report.bandwidth_snapshots, report.state_snapshots), (1, 1))
            self.assertTrue(report.backup_paths)
            cleaned = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertNotIn("users", cleaned)
            self.assertNotIn("tg_lang", cleaned["publicbot"])
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            db = Database(str(db_path))
            self.assertEqual(db.tgid_to_user(123), "alice")
            self.assertEqual(db.get_telegram_language(123), "en")
            self.assertTrue(db.notification_seen("regular", 123))
            self.assertEqual(len(db.get_bandwidth_snapshots("alice", 0)), 1)
            self.assertEqual(len(db.get_state_snapshots(0)), 1)
            db.close()
            rerun = migrate_legacy(db_path=str(db_path), config_path=str(config_path), bandwidth_path=str(bw_path), snapshots_path=str(snapshots_path))
            self.assertTrue(rerun.already_migrated)

    def test_failed_import_rolls_back_and_keeps_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            db_path = root / "state.sqlite3"
            shared_token = "t" * 40
            legacy = {
                "users": {"alice": str(uuid.uuid4()), "bob": str(uuid.uuid4())},
                "tokens": {"alice": shared_token, "bob": shared_token},
                "userFingerprints": {"alice": "chrome", "bob": "chrome"},
                "displaynames": {"alice": "Alice", "bob": "Bob"},
                "bw": {"alice": [1, 0], "bob": [1, 0]},
                "wl_bw": {"alice": [1, 0], "bob": [1, 0]},
            }
            config_path.write_text(json.dumps(legacy), encoding="utf-8")
            with self.assertRaises(DatabaseError):
                migrate_legacy(db_path=str(db_path), config_path=str(config_path))
            self.assertIn("users", json.loads(config_path.read_text(encoding="utf-8")))
            database = Database(str(db_path))
            self.assertEqual(database.list_users(), [])
            self.assertIsNone(database.get_metadata("legacy_migration"))
            database.close()
            self.assertTrue(tuple((root / "migration-backups").iterdir()))

    def test_duplicate_snapshot_timestamps_keep_last_legacy_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            bandwidth_path = root / "bw.json"
            snapshots_path = root / "snapshots.json"
            db_path = root / "state.sqlite3"
            config_path.write_text(json.dumps({
                "users": {"alice": "00000000-0000-4000-8000-000000000001"},
                "tokens": {"alice": "t" * 40},
                "userFingerprints": {"alice": "chrome"},
                "displaynames": {"alice": "Alice"},
                "bw": {"alice": [0, 0]},
                "wl_bw": {"alice": [0, 0]},
            }), encoding="utf-8")
            bandwidth_path.write_text(json.dumps({
                "users": {"alice": {"snapshots": [
                    {"ts": 10, "up": 1, "down": 2},
                    {"ts": 10, "up": -3, "down": 4},
                ]}},
            }), encoding="utf-8")
            snapshots_path.write_text(json.dumps({"snapshots": [
                {"ts": 10, "host": {"version": 1}, "panels": {}},
                {"ts": 10, "host": {"version": 2}, "panels": {}},
            ]}), encoding="utf-8")

            report = migrate_legacy(
                db_path=str(db_path), config_path=str(config_path),
                bandwidth_path=str(bandwidth_path), snapshots_path=str(snapshots_path),
            )
            self.assertEqual((report.bandwidth_snapshots, report.state_snapshots), (1, 1))
            database = Database(str(db_path))
            self.assertEqual(database.get_bandwidth_snapshots("alice", 0)[0]["up"], -3)
            self.assertEqual(database.get_state_snapshots(0)[0]["host"], {"version": 2})
            database.close()

    def test_invalid_legacy_types_are_not_coerced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            db_path = root / "state.sqlite3"
            config_path.write_text(json.dumps({
                "users": {"alice": "00000000-0000-4000-8000-000000000001"},
                "tokens": {"alice": "t" * 40},
                "userFingerprints": {"alice": "chrome"},
                "displaynames": {"alice": "Alice"},
                "status": {"alice": "false"},
                "bw": {"alice": [0, 0]},
                "wl_bw": {"alice": [0, 0]},
            }), encoding="utf-8")

            with self.assertRaisesRegex(MigrationError, "status.*boolean"):
                migrate_legacy(db_path=str(db_path), config_path=str(config_path))
            self.assertIn("users", json.loads(config_path.read_text(encoding="utf-8")))
            self.assertFalse(db_path.exists())


if __name__ == "__main__":
    unittest.main()
