from __future__ import annotations

from typing import Any, cast
from flask import Flask
import json
import logging
import tempfile
import unittest
from argon2 import PasswordHasher
from dataclasses import asdict
from requests import Response

from core import Subscription
from custom_types import ConfigLike, Inbound
from session import XUiSession
from db import Database
from errors import ConflictError, NotFoundError, PanelRejectedError, ValidationError


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


class RejectedPanelTests(unittest.TestCase):
    def test_add_users_raises_panel_rejected_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            config: dict[str, Any] = {
                "uri": "sub",
                "fingerprints": ["chrome"],
                "salt": "test-salt",
                "domain": "https://example.test",
                "funny_strings": ["test"],
            }
            subscription = Subscription(
                cfg=cast(ConfigLike, config), lang_cfg=cast(ConfigLike, {}), db=database,
                app=Flask(__name__), panels=[], whitelist_panel=None,
            )
            database.create_user(
                username="alice", uuid="01234567-89ab-cdef-0123-456789abcdef",
                token="a" * 40, fingerprint="chrome", displayname="Alice",
                ext_username="alice-login", ext_password_hash="hash",
            )
            inbound = Inbound(
                id=1, up=0, down=0, total=0, allTime=0, remark="test", enable=True,
                expiryTime=0, trafficReset="", lastTrafficResetTime=0, clientStats=[],
                listen="", port=443, protocol="vless", settings="{}",
                streamSettings=json.dumps({"network": "tcp"}), tag="", sniffing="",
            )

            class RejectedPanel:
                local = False
                ignore_inbounds = ()
                cache: list[Inbound] | None = None
                cache_time = 0

                def get(self, url: str) -> Response:
                    response = Response()
                    response.status_code = 200
                    response.headers["Content-Type"] = "application/json"
                    response._content = json.dumps(
                        {"success": True, "msg": "", "obj": [asdict(inbound)]}
                    ).encode()
                    return response

                def post(self, url: str, **kwargs: object) -> Response:
                    response = Response()
                    response.status_code = 201
                    response.headers["Content-Type"] = "application/json"
                    response._content = json.dumps(
                        {"success": False, "msg": "rejected", "obj": None}
                    ).encode()
                    return response

            subscription.panels.append(cast(XUiSession, RejectedPanel()))
            with self.assertRaises(PanelRejectedError):
                subscription.add_users("alice")
            database.close()


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

            self.assertIsNone(subscription.validate_credentials("alice-login", "wrong"))
            self.assertEqual(
                subscription.validate_credentials("alice-login", "secret"), "alice"
            )
            database.close()

    def test_valid_legacy_hash_is_migrated_to_argon2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            subscription = self._subscription(directory, database)
            self._create_user(database, subscription.legacy_hash("secret"))

            self.assertEqual(
                subscription.validate_credentials("alice-login", "secret"), "alice"
            )
            stored = database.ext_password("alice-login")
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertTrue(stored.startswith("$argon2id$"))

            subscription.SALT = "changed-legacy-salt"
            self.assertEqual(
                subscription.validate_credentials("alice-login", "secret"), "alice"
            )
            database.close()

    def test_validate_credentials_logs_corrupt_argon2_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            subscription = self._subscription(directory, database)
            self._create_user(database, "$argon2id$corrupt")

            subscription.log.addHandler(logging.NullHandler())
            with self.assertLogs(subscription.log, level="ERROR") as logs:
                self.assertIsNone(
                    subscription.validate_credentials("alice-login", "secret")
                )
            self.assertIn("Corrupt or tampered argon2 password hash", logs.output[0])
            database.close()


if __name__ == "__main__":
    unittest.main()
