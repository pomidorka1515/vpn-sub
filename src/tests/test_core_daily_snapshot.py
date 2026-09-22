from __future__ import annotations

from typing import Any, cast
from flask import Flask
import tempfile
import unittest
import uuid

from core import Subscription
from bwatch import BWatch
from config import ConfigLike
from custom_types import BandwidthInfo
from db import Database
from errors import PanelUnavailableError


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

            subscription.bandwidth_svc.bandwidth = fail_bandwidth  # type: ignore[method-assign]
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

            subscription.bandwidth_svc.bandwidth = bandwidth  # type: ignore[method-assign]
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

            subscription.bandwidth_svc.bandwidth = bandwidth  # type: ignore[method-assign]
            watch.record_daily_snapshot()
            failure = watch.get_daily_snapshot_failure()
            assert failure is not None
            self.assertEqual((failure["failed"], failure["eligible"]), (1, 2))
            database.close()

if __name__ == "__main__":
    unittest.main()
