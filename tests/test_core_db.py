from __future__ import annotations

from typing import Any, cast
from flask import Flask
import json
import logging
import tempfile
import unittest
import uuid
from argon2 import PasswordHasher
from dataclasses import asdict
from requests import Response

from core import Subscription
from bwatch import BWatch
from protocols import ConfigLike
from custom_types import BandwidthInfo, Inbound
from session import XUiSession
from db import Database
from errors import (
    ConflictError, NotFoundError, PanelRejectedError, PanelUnavailableError, ValidationError
)


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


class OnlineStatusTests(unittest.TestCase):
    @staticmethod
    def _subscription(database: Database) -> Subscription:
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

    def test_online_status_reports_empty_when_all_panels_succeed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            subscription = self._subscription(database)

            class EmptyPanel:
                name = "panel"
                dead = False

                def post(self, url: str, **kwargs: object) -> Response:
                    response = Response()
                    response.status_code = 200
                    response.headers["Content-Type"] = "application/json"
                    response._content = json.dumps({"success": True, "obj": []}).encode()
                    return response

            subscription.panels.append(cast(XUiSession, EmptyPanel()))
            status = subscription.get_online_status()
            self.assertEqual(status.users, [])
            self.assertEqual(status.panel_health, {"panel": "ok"})
            database.close()

    def test_online_status_raises_when_all_panels_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            subscription = self._subscription(database)

            class FailedPanel:
                name = "panel"
                dead = False

                def post(self, url: str, **kwargs: object) -> Response:
                    raise RuntimeError("transport failed")

            subscription.panels.append(cast(XUiSession, FailedPanel()))
            with self.assertRaises(PanelUnavailableError):
                subscription.get_online_status()
            database.close()

    def test_online_status_reports_empty_without_panels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            subscription = self._subscription(database)
            status = subscription.get_online_status()
            self.assertEqual(status.users, [])
            self.assertEqual(status.panel_health, {})
            self.assertEqual(subscription.get_online_status(new=True).users, {})
            database.close()

    def test_online_status_marks_malformed_payload_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            subscription = self._subscription(database)

            class InvalidPanel:
                name = "panel"
                dead = False

                def post(self, url: str, **kwargs: object) -> Response:
                    response = Response()
                    response.status_code = 200
                    response.headers["Content-Type"] = "application/json"
                    response._content = json.dumps({"success": True, "obj": 7}).encode()
                    return response

            subscription.panels.append(cast(XUiSession, InvalidPanel()))
            status = subscription.get_online_status()
            self.assertEqual(status.panel_health, {"panel": "invalid"})
            database.close()


class BandwidthPollTests(unittest.TestCase):
    def test_partial_counter_failure_does_not_advance_either_baseline(self) -> None:
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
            database.create_user(
                username="alice", uuid="01234567-89ab-cdef-0123-456789abcdef",
                token="a" * 40, fingerprint="chrome", displayname="Alice",
                bw_limit_gb=1, wl_limit_gb=1,
            )
            class BWatchConfig:
                def get(self, key: str, *args: object, **kwargs: object) -> object:
                    if key == "panel_alert_cooldown":
                        return 3600
                    raise KeyError(key)

            watch = BWatch(
                cfg=cast(ConfigLike, BWatchConfig()), db=database,
                sub=subscription, bot=None, admin_bot=None,
            )
            first = BandwidthInfo(upload=100, download=100, total=200)
            second = BandwidthInfo(upload=200, download=200, total=400)
            calls: list[bool] = []

            def bandwidth(username: str, whitelist: bool = False) -> BandwidthInfo:
                calls.append(whitelist)
                if whitelist:
                    raise RuntimeError("whitelist panel failed")
                return second

            watch.mem["alice"] = first
            watch.wl_mem["alice"] = first
            subscription.bandwidth = bandwidth  # type: ignore[method-assign]
            watch.bandwidth_check()

            self.assertEqual(calls, [False, True])
            self.assertEqual(watch.mem["alice"], first)
            self.assertEqual(watch.wl_mem["alice"], first)
            self.assertEqual(int(subscription.get_user_state("alice")["bw_used"]), 0)
            self.assertEqual(int(subscription.get_user_state("alice")["wl_used"]), 0)
            database.close()


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
            self._subscription(database)
            self.assertIsNone(database.get_metadata("registration_rollback_failed:alice"))
            self.assertFalse(database.user_exists("alice"))
            database.close()

    def test_rollback_marker_reporting_and_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            subscription = self._subscription(database)
            database.set_metadata("uuid_rollback_failed:alice", "123:panel rejected")
            database.set_metadata("registration_rollback_failed:bob", "124")
            failures = subscription.get_rollback_failures()
            self.assertEqual(failures["uuid"]["alice"]["reason"], "panel rejected")
            self.assertEqual(failures["registration"]["bob"]["ts"], "124")
            subscription.clear_rollback_failure("uuid", "alice")
            self.assertEqual(subscription.get_rollback_failures()["uuid"], {})
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

            subscription.add_users = fail_sync  # type: ignore[method-assign]
            database.rollback_registration_sync = fail_rollback  # type: ignore[method-assign]
            with self.assertRaises(RuntimeError) as raised:
                subscription.register_with_code(
                    code="invite", username="alice", displayname="Alice",
                    ext_username="alice-login", ext_password="secret",
                )
            self.assertIs(raised.exception, sync_error)
            self.assertIsNotNone(
                database.get_metadata("registration_rollback_failed:alice")
            )
            database.close()


class DailySnapshotTests(unittest.TestCase):
    def _subscription(self, database: Database) -> Subscription:
        config: dict[str, Any] = {
            "uri": "sub", "fingerprints": ["chrome"], "salt": "test-salt",
            "domain": "https://example.test", "funny_strings": ["test"],
        }
        return Subscription(
            cfg=cast(ConfigLike, config), lang_cfg=cast(ConfigLike, {}), db=database,
            app=Flask(__name__), panels=[], whitelist_panel=None,
        )

    def _watch(self, database: Database, subscription: Subscription) -> BWatch:
        class BWatchConfig:
            def get(self, key: str, *args: object, **kwargs: object) -> object:
                if key == "panel_alert_cooldown":
                    return 3600
                raise KeyError(key)

        return BWatch(
            cfg=cast(ConfigLike, BWatchConfig()), db=database,
            sub=subscription, bot=None, admin_bot=None,
        )

    def test_daily_snapshot_retries_only_failed_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(path=f"{directory}/state.sqlite3")
            watch = self._watch(database, self._subscription(database))
            bandwidth_calls: list[str] = []
            state_calls: list[str] = []

            def record_bandwidth() -> None:
                bandwidth_calls.append("call")
                if len(bandwidth_calls) == 1:
                    raise PanelUnavailableError("panel unavailable")

            def record_state() -> None:
                state_calls.append("call")

            watch.record_daily_snapshot = record_bandwidth  # type: ignore[method-assign]
            watch.record_snap_snapshot = record_state  # type: ignore[method-assign]
            watch._snapshot_due_at = {"bandwidth": 0.0, "state": 0.0}  # pyright: ignore[reportPrivateUsage]

            watch._run_due_daily_snapshots(100.0)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual((len(bandwidth_calls), len(state_calls)), (1, 1))

            watch._snapshot_due_at = {"bandwidth": 3600.0, "state": 86400.0}  # pyright: ignore[reportPrivateUsage]
            watch._run_due_daily_snapshots(3600.0)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual((len(bandwidth_calls), len(state_calls)), (2, 1))

            watch._snapshot_due_at = {"bandwidth": 7200.0, "state": 86400.0}  # pyright: ignore[reportPrivateUsage]
            watch._run_due_daily_snapshots(7200.0)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual((len(bandwidth_calls), len(state_calls)), (3, 1))
            database.close()

    def test_all_daily_snapshot_failures_raise_and_persist_counts(self) -> None:
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
            database.create_user(
                username="alice", uuid="01234567-89ab-cdef-0123-456789abcdef",
                token="a" * 40, fingerprint="chrome", displayname="Alice",
                bw_limit_gb=1, wl_limit_gb=1,
            )
            watch = self._watch(database, subscription)

            def fail_bandwidth(username: str, whitelist: bool = False) -> BandwidthInfo:
                raise RuntimeError("panel unavailable")

            subscription.bandwidth = fail_bandwidth  # type: ignore[method-assign]
            with self.assertRaises(PanelUnavailableError):
                watch.record_daily_snapshot()
            failure = watch.get_daily_snapshot_failure()
            assert failure is not None
            self.assertEqual((failure["failed"], failure["eligible"]), (1, 1))
            database.close()

    def test_successful_daily_snapshot_clears_previous_failure(self) -> None:
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
            database.create_user(
                username="alice", uuid=str(uuid.uuid4()), token="a" * 40,
                fingerprint="chrome", displayname="Alice",
                bw_limit_gb=1, wl_limit_gb=1,
            )
            watch = self._watch(database, subscription)
            database.set_metadata("daily_bw_snapshot_failures", "123:1:1")

            def bandwidth(username: str, whitelist: bool = False) -> BandwidthInfo:
                return BandwidthInfo(1, 2, 3)

            subscription.bandwidth = bandwidth  # type: ignore[method-assign]
            watch.record_daily_snapshot()
            self.assertIsNone(watch.get_daily_snapshot_failure())
            database.close()

    def test_partial_daily_snapshot_failure_is_visible(self) -> None:
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
            for username in ("alice", "bob"):
                database.create_user(
                    username=username, uuid=str(uuid.uuid4()),
                    token=username.ljust(40, "x"), fingerprint="chrome",
                    displayname=username.title(), bw_limit_gb=1, wl_limit_gb=1,
                )
            watch = self._watch(database, subscription)

            def bandwidth(username: str, whitelist: bool = False) -> BandwidthInfo:
                if username == "alice":
                    raise RuntimeError("panel unavailable")
                return BandwidthInfo(1, 2, 3)

            subscription.bandwidth = bandwidth  # type: ignore[method-assign]
            watch.record_daily_snapshot()
            failure = watch.get_daily_snapshot_failure()
            assert failure is not None
            self.assertEqual((failure["failed"], failure["eligible"]), (1, 2))
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
