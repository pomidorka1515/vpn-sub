from __future__ import annotations

from typing import Any, cast
from flask import Flask
import tempfile
import unittest

from core import Subscription
from custom_types import ConfigLike
from db import Database
from errors import ConflictError, NotFoundError, ValidationError


class SubscriptionDatabaseWorkflowTests(unittest.TestCase):
    def test_user_workflow_without_live_panels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            config: dict[str, Any] = {
                "uri": "sub",
                "fingerprints": ["chrome", "firefox"],
                "salt": "test-salt",
                "domain": "https://example.test",
                "funny_strings": ["test"],
            }
            subscription = Subscription(
                cfg=cast(ConfigLike, config), lang_cfg=cast(ConfigLike, {}), db=database, app=Flask(__name__),
                panels=[], whitelist_panel=None,
            )
            with self.assertRaises(NotFoundError):
                subscription.get_info("missing")
            created = subscription.add_new_user(
                "alice", "Alice", ext_username="alice-login",
                ext_password="secret", limit=10, wl_limit=5,
            )
            with self.assertRaises(ValidationError):
                subscription.update_params("alice", fingerprint="invalid")
            with self.assertRaises(ConflictError):
                subscription.add_new_user(
                    "alice", "Alice", ext_username="alice-login",
                    ext_password="secret", limit=10, wl_limit=5,
                )
            with self.assertRaises(ValidationError):
                subscription.add_code("", "bonus")
            subscription.add_code("invite", "bonus")
            with self.assertRaises(ConflictError):
                subscription.add_code("invite", "bonus")
            with self.assertRaises(NotFoundError):
                subscription.get_code("missing")
            with self.assertRaises(NotFoundError):
                subscription.delete_code("missing")
            self.assertNotIsInstance(created, str)
            self.assertEqual(subscription.validate_credentials("alice-login", "secret"), "alice")
            subscription.set_telegram_user(123, "alice")
            self.assertEqual(subscription.get_username_telegram(123), "alice")
            subscription.update_params("alice", displayname="Alice 2", fingerprint="firefox")
            info = subscription.get_info("alice")
            self.assertEqual((info.displayname, info.fingerprint), ("Alice 2", "firefox"))
            subscription.delete_user("alice", perma=True)
            self.assertFalse(subscription.isuser("alice"))
            database.close()


if __name__ == "__main__":
    unittest.main()
