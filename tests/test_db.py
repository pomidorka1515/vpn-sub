from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import uuid

from errors import CodeError, DuplicateError, MigrationError
from db import Database

class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.db = Database(path=str(self.path), timeout=2)

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
        self.db = Database(path=str(self.path))
        self.assertEqual(self.db.list_users(), [])
        self.assertEqual(self.db.get_metadata("missing"), None)

    def test_unversioned_existing_database_is_refused(self) -> None:
        path = Path(self.temp.name) / "unversioned.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE users(username TEXT PRIMARY KEY)")
        connection.close()
        with self.assertRaises(MigrationError):
            Database(path=str(path))

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

    def test_metadata_prefix_listing_and_deletion(self) -> None:
        self.db.set_metadata("prefix_one", "1")
        self.db.set_metadata("prefix_two", "2")
        self.db.set_metadata("prefix_100%", "3")
        self.db.set_metadata("other", "4")
        self.assertEqual(
            self.db.list_metadata("prefix_"),
            {"prefix_one": "1", "prefix_two": "2", "prefix_100%": "3"},
        )
        self.db.delete_metadata("prefix_one")
        self.assertIsNone(self.db.get_metadata("prefix_one"))
        self.assertEqual(self.db.get_metadata("prefix_two"), "2")

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


if __name__ == "__main__":
    unittest.main()
