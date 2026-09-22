from __future__ import annotations

from typing import Any, cast
from flask import Flask
import tempfile
import unittest

from core import Subscription
from config import ConfigLike
from db import Database


class SubscriptionAuthSessionTests(unittest.TestCase):
    def test_reset_user_invalidates_auth_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            config: dict[str, Any] = {
                "uri": "sub", "fingerprints": ["chrome"], "salt": "test-salt",
                "domain": "https://example.test", "funny_strings": ["test"],
            }
            subscription = Subscription(
                cfg=cast(ConfigLike, config), lang_cfg=cast(ConfigLike, {}),
                db=database, app=Flask(__name__), panels=[], whitelist_panel=None,
            )
            database.create_user(
                username="alice", uuid="01234567-89ab-cdef-0123-456789abcdef",
                token="a" * 40, fingerprint="chrome", displayname="Alice",
            )
            database.set_auth_token("alice", "b" * 100)

            result = subscription.business_svc.reset_user("alice")

            self.assertNotEqual(result.token, "a" * 40)
            self.assertEqual(subscription.user_svc.get_token("alice"), result.token)
            record = database.get_user("alice")
            assert record is not None
            self.assertIsNone(record["auth_token"])
            self.assertIsNone(subscription.user_svc.auth_token_to_user("b" * 100))
            database.close()

if __name__ == "__main__":
    unittest.main()
