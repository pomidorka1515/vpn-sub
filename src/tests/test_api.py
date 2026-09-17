from __future__ import annotations

import tempfile
import unittest
import uuid
from typing import Any, cast

from api import WebApi
from bwatch import BWatch
from core import Subscription
from argon2 import PasswordHasher
from db import Database
from flask import Flask
from config import ConfigLike


class _BWatchConfig:
    def get(self, key: str, *args: object, **kwargs: object) -> object:
        if key == "panel_alert_cooldown":
            return 3600
        raise KeyError(key)


class WebApiAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.password_hash = PasswordHasher().hash("secret")
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(path=f"{self.temporary.name}/state.sqlite3")
        self.app = Flask(__name__)
        config: dict[str, Any] = {
            "uri": "sub",
            "fingerprints": ["chrome"],
            "salt": "test-salt",
            "domain": "https://example.test",
            "funny_strings": ["test"],
        }
        self.subscription = Subscription(
            cfg=cast(ConfigLike, config), lang_cfg=cast(ConfigLike, {}),
            db=self.database, app=self.app, panels=[], whitelist_panel=None,
        )
        self.database.create_user(
            username="alice", uuid=str(uuid.uuid4()), token="a" * 40,
            fingerprint="chrome", displayname="Alice",
            ext_username="alice-login", ext_password_hash=self.password_hash,
        )
        self.subscription.hash = lambda value: self.password_hash  # type: ignore[assignment]
        self.watcher = BWatch(
            cfg=cast(ConfigLike, _BWatchConfig()), db=self.database,
            sub=self.subscription, bot=None, admin_bot=None,
        )
        self.web_api = WebApi(
            app=self.app, cfg=cast(ConfigLike, config),
            sub=self.subscription, bw=self.watcher,
        )

    def tearDown(self) -> None:
        self.database.close()
        self.temporary.cleanup()

    def test_login_uses_isolated_auth_token(self) -> None:
        client = self.app.test_client()
        legacy_cookie_response = client.get(
            "/sub/webapi/stats", headers={"Cookie": "token=" + "a" * 40}
        )
        self.assertEqual(legacy_cookie_response.status_code, 401)

        login_response = client.post(
            "/sub/webapi/login",
            json={"username": "alice-login", "password": "secret"},
        )
        self.assertEqual(login_response.status_code, 200)
        cookies = login_response.headers.getlist("Set-Cookie")
        self.assertTrue(any(cookie.startswith("auth_token=") for cookie in cookies))
        self.assertTrue(any(cookie.startswith("token=;") for cookie in cookies))
        auth_cookie = next(
            cookie for cookie in cookies if cookie.startswith("auth_token=")
        )
        self.assertIn("Max-Age=2592000", auth_cookie)
        self.assertIn("HttpOnly", auth_cookie)
        self.assertIn("Secure", auth_cookie)
        self.assertIn("SameSite=Lax", auth_cookie)
        auth_token = auth_cookie.split("=", 1)[1].split(";", 1)[0]
        self.assertEqual(len(auth_token), 100)
        self.assertNotEqual(auth_token, "a" * 40)
        self.assertNotIn("auth_token", login_response.get_json()["obj"])

        stats_response = client.get("/sub/webapi/stats")
        self.assertEqual(stats_response.status_code, 200)
        self.assertEqual(stats_response.get_json()["obj"]["token"], "a" * 40)

    def test_logout_invalidates_auth_token(self) -> None:
        self.database.set_auth_token("alice", "a" * 100)
        client = self.app.test_client()
        client.set_cookie("auth_token", "a" * 100)
        response = client.post("/sub/webapi/logout")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Set-Cookie"].count("auth_token="), 1)
        record = self.database.get_user("alice")
        assert record is not None
        self.assertIsNone(record["auth_token"])
        self.assertEqual(client.get("/sub/webapi/stats").status_code, 401)


if __name__ == "__main__":
    unittest.main()
