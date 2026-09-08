from __future__ import annotations

from typing import Any, cast
from flask import Flask
import tempfile
import unittest

from core import Subscription
from custom_types import ConfigLike
from db import Database


class SubscriptionDatabaseWorkflowTests(unittest.TestCase):
    def test_user_workflow_without_live_panels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"{directory}/state.sqlite3")
            config: dict[str, Any] = {
                "uri": "sub",
                "fingerprints": ["chrome", "firefox"],
                "salt": "test-salt",
                "domain": "https://example.test",
                "funny_strings": ["test"],
            }
            subscription = Subscription(
                cfg=cast(ConfigLike, config), db=database, app=Flask(__name__),
                panels=[], whitelist_panel=None,
            )
            created = subscription.add_new_user(
                "alice", "Alice", ext_username="alice-login",
                ext_password="secret", limit=10, wl_limit=5,
            )
            self.assertNotIsInstance(created, str)
            self.assertEqual(subscription.validate_credentials("alice-login", "secret"), "alice")
            subscription.set_telegram_user(123, "alice")
            self.assertEqual(subscription.get_username_telegram(123), "alice")
            self.assertIsNone(subscription.update_params("alice", displayname="Alice 2", fingerprint="firefox"))
            info = subscription.get_info("alice")
            assert info is not None
            self.assertEqual((info.displayname, info.fingerprint), ("Alice 2", "firefox"))
            self.assertIsNone(subscription.delete_user("alice", perma=True))
            self.assertFalse(subscription.isuser("alice"))
            database.close()


if __name__ == "__main__":
    unittest.main()
