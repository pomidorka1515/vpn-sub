from __future__ import annotations

from typing import Any, cast
from flask import Flask
import tempfile
import unittest
import uuid

from core import Subscription
from config import ConfigLike
from db import Database


class RollbackTests(unittest.TestCase):
    def _subscription(self, database: Database) -> Subscription:
        config: dict[str, Any] = {
            "uri": "sub", "fingerprints": ["chrome"], "salt": "test-salt",
            "domain": "https://example.test", "funny_strings": ["test"],
        }
        return Subscription(
            cfg=cast(ConfigLike, config), lang_cfg=cast(ConfigLike, {}), db=database,
            app=Flask(__name__), panels=[], whitelist_panel=None,
        )

    def test_startup_recovers_registration_rollback_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            database.add_code("invite", "register", uses=1)
            database.register_with_code(
                code="invite", username="alice", uuid=str(uuid.uuid4()),
                token="a" * 40, fingerprint="chrome", displayname="Alice",
                ext_username="alice-login", ext_password_hash="hash",
            )
            database.set_metadata("registration_rollback_failed:alice", "123")
            subscription = self._subscription(database)
            subscription.business_code_svc.recover_rollback_failures()
            self.assertIsNone(database.get_metadata("registration_rollback_failed:alice"))
            self.assertFalse(database.user_exists("alice"))
            database.close()

    def test_rollback_marker_reporting_and_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            subscription = self._subscription(database)
            database.set_metadata("uuid_rollback_failed:alice", "123:panel rejected")
            database.set_metadata("registration_rollback_failed:bob", "124")
            failures = subscription.business_code_svc.get_rollback_failures()
            self.assertEqual(failures["uuid"]["alice"]["reason"], "panel rejected")
            self.assertEqual(failures["registration"]["bob"]["ts"], "124")
            subscription.business_code_svc.clear_rollback_failure("uuid", "alice")
            self.assertEqual(subscription.business_code_svc.get_rollback_failures()["uuid"], {})
            database.close()

    def test_registration_rollback_failure_preserves_sync_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            config: dict[str, Any] = {
                "uri": "sub", "fingerprints": ["chrome"], "salt": "test-salt",
                "domain": "https://example.test", "funny_strings": ["test"],
            }
            subscription = Subscription(
                cfg=cast(ConfigLike, config), lang_cfg=cast(ConfigLike, {}), db=database,
                app=Flask(__name__), panels=[], whitelist_panel=None,
            )
            database.add_code("invite", "register", days=1, gb=1, wl_gb=0, permanent=False, uses=1)
            sync_error = RuntimeError("panel transport failed")

            def fail_sync(username: str, _called_internally: bool = False) -> None:
                raise sync_error

            def fail_rollback(username: str) -> None:
                raise RuntimeError("database rollback failed")

            subscription.business_svc.add_users = fail_sync  # type: ignore[method-assign]
            database.rollback_registration_sync = fail_rollback  # type: ignore[method-assign]
            with self.assertRaises(RuntimeError) as raised:
                subscription.business_code_svc.register_with_code(
                    code="invite", username="alice", displayname="Alice",
                    ext_username="alice-login", ext_password="secret",
                )
            self.assertIs(raised.exception, sync_error)
            self.assertIsNotNone(
                database.get_metadata("registration_rollback_failed:alice")
            )
            database.close()

if __name__ == "__main__":
    unittest.main()
