from __future__ import annotations

from typing import Any, cast
from flask import Flask
import logging
import tempfile
import unittest
from argon2 import PasswordHasher
from dataclasses import replace as dreplace

from core import Subscription
from config import ConfigLike
from db import Database


class CredentialValidationTests(unittest.TestCase):
    def _subscription(self, directory: str, database: Database) -> Subscription:
        config: dict[str, Any] = {
            "uri": "sub",
            "fingerprints": ["chrome"],
            "salt": "test-salt",
            "domain": "https://example.test",
            "funny_strings": ["test"],
        }
        return Subscription(
            cfg=cast(ConfigLike, config), lang_cfg=cast(ConfigLike, {}), db=database,
            app=Flask(__name__), panels=[], whitelist_panel=None,
        )

    def _create_user(self, database: Database, password_hash: str) -> None:
        database.create_user(
            username="alice", uuid="01234567-89ab-cdef-0123-456789abcdef",
            token="a" * 40, fingerprint="chrome", displayname="Alice",
            ext_username="alice-login", ext_password_hash=password_hash,
        )

    def test_validate_credentials_rejects_wrong_argon2_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            subscription = self._subscription(directory, database)
            self._create_user(database, PasswordHasher().hash("secret"))

            self.assertIsNone(subscription.password_svc.validate_credentials("alice-login", "wrong"))
            self.assertEqual(
                subscription.password_svc.validate_credentials("alice-login", "secret"), "alice"
            )
            database.close()

    def test_valid_legacy_hash_is_migrated_to_argon2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            subscription = self._subscription(directory, database)
            self._create_user(database, subscription.password_svc.legacy_hash("secret"))

            self.assertEqual(
                subscription.password_svc.validate_credentials("alice-login", "secret"), "alice"
            )
            stored = database.ext_password("alice-login")
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertTrue(stored.startswith("$argon2id$"))

            subscription.res = dreplace(subscription.res, legacy_salt="changed-legacy-salt")
            self.assertEqual(
                subscription.password_svc.validate_credentials("alice-login", "secret"), "alice"
            )
            database.close()

    def test_validate_credentials_logs_corrupt_argon2_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            subscription = self._subscription(directory, database)
            self._create_user(database, "$argon2id$corrupt")

            subscription.res.log.addHandler(logging.NullHandler())
            with self.assertLogs(subscription.res.log, level="ERROR") as logs:
                self.assertIsNone(
                    subscription.password_svc.validate_credentials("alice-login", "secret")
                )
            self.assertIn("Corrupt or tampered argon2 password hash", logs.output[0])
            database.close()

if __name__ == "__main__":
    unittest.main()
