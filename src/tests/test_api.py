from __future__ import annotations

import tempfile
import unittest
import uuid
from typing import Any, cast
from unittest import mock

from api import BaseApi, WebApi, _RateLimiter, rate_limit  # pyright: ignore[reportPrivateUsage]
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
        self.subscription.password_svc.hash = lambda value: self.password_hash  # type: ignore[assignment]
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


class RateLimitTests(unittest.TestCase):
    def test_rejects_non_positive_limit(self) -> None:
        with self.assertRaises(ValueError):
            rate_limit(0)

        with self.assertRaises(ValueError):
            rate_limit(1, max_buckets=0)

    def test_limits_each_ip_independently_without_instance_state(self) -> None:
        class Handler(BaseApi):
            ROUTES = []

            @rate_limit(2)
            def endpoint(self: BaseApi) -> tuple[str, int]:
                return "ok", 200

        handler = object.__new__(Handler)
        with self.app_context("203.0.113.1"):
            with mock.patch("api.time.monotonic", side_effect=[0.0, 1.0, 2.0]):
                self.assertEqual(handler.endpoint(), ("ok", 200))
                self.assertEqual(handler.endpoint(), ("ok", 200))
                _, status = handler.endpoint()
        self.assertEqual(status, 429)

        with self.app_context("203.0.113.2"):
            with mock.patch("api.time.monotonic", return_value=2.0):
                self.assertEqual(handler.endpoint(), ("ok", 200))

    def test_window_expires_at_sixty_seconds(self) -> None:
        class Handler(BaseApi):
            ROUTES = []

            @rate_limit(1)
            def endpoint(self: BaseApi) -> tuple[str, int]:
                return "ok", 200

        handler = object.__new__(Handler)
        with self.app_context("203.0.113.3"):
            with mock.patch("api.time.monotonic", side_effect=[0.0, 59.999, 60.0]):
                self.assertEqual(handler.endpoint(), ("ok", 200))
                self.assertEqual(handler.endpoint()[1], 429)
                self.assertEqual(handler.endpoint(), ("ok", 200))

    def test_inactive_buckets_are_evicted_without_global_scan(self) -> None:
        limiter = _RateLimiter(1)
        self.assertTrue(limiter.allow("first", 0.0))
        self.assertTrue(limiter.allow("second", 1.0))
        self.assertEqual(limiter.bucket_count, 2)

        self.assertTrue(limiter.allow("third", 62.0))
        self.assertEqual(limiter.bucket_keys(), ("third",))

    def test_rejected_request_does_not_break_eviction_order(self) -> None:
        limiter = _RateLimiter(1)
        self.assertTrue(limiter.allow("first", 0.0))
        self.assertTrue(limiter.allow("second", 1.0))
        self.assertFalse(limiter.allow("first", 59.0))

        self.assertTrue(limiter.allow("third", 60.5))
        self.assertEqual(limiter.bucket_keys(), ("second", "third"))

    def test_reused_decorator_gives_each_endpoint_its_own_limiter(self) -> None:
        limiter_decorator = rate_limit(1)

        class Handler(BaseApi):
            ROUTES = []

            @limiter_decorator
            def first(self: BaseApi) -> tuple[str, int]:
                return "first", 200

            @limiter_decorator
            def second(self: BaseApi) -> tuple[str, int]:
                return "second", 200

        handler = object.__new__(Handler)
        with self.app_context("203.0.113.4"):
            with mock.patch("api.time.monotonic", return_value=0.0):
                self.assertEqual(handler.first(), ("first", 200))
                self.assertEqual(handler.second(), ("second", 200))

    def test_bucket_count_is_capped(self) -> None:
        limiter = _RateLimiter(1, max_buckets=2)
        self.assertTrue(limiter.allow("first", 0.0))
        self.assertTrue(limiter.allow("second", 1.0))
        self.assertTrue(limiter.allow("third", 2.0))

        self.assertEqual(limiter.bucket_count, 2)
        self.assertEqual(limiter.bucket_keys(), ("second", "third"))

    def test_missing_remote_address_uses_shared_unknown_bucket(self) -> None:
        class Handler(BaseApi):
            ROUTES = []

            @rate_limit(1)
            def endpoint(self: BaseApi) -> tuple[str, int]:
                return "ok", 200

        handler = object.__new__(Handler)
        with self.app_context(None):
            with mock.patch("api.time.monotonic", side_effect=[0.0, 1.0]):
                self.assertEqual(handler.endpoint(), ("ok", 200))
                self.assertEqual(handler.endpoint()[1], 429)

    def setUp(self) -> None:
        self.app = Flask(__name__)

    def app_context(self, remote_addr: str | None) -> Any:
        return self.app.test_request_context(
            "/",
            environ_base={"REMOTE_ADDR": remote_addr} if remote_addr is not None else {},
        )


if __name__ == "__main__":
    unittest.main()
