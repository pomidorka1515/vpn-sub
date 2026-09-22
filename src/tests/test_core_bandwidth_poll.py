from __future__ import annotations

from typing import Any, cast
from flask import Flask
import tempfile
import unittest

from core import Subscription
from bwatch import BWatch
from config import ConfigLike
from custom_types import BandwidthInfo
from db import Database


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
            subscription.bandwidth_svc.bandwidth = bandwidth  # type: ignore[method-assign]
            watch.bandwidth_check()

            self.assertEqual(calls, [False, True])
            self.assertEqual(watch.mem["alice"], first)
            self.assertEqual(watch.wl_mem["alice"], first)
            self.assertEqual(int(subscription.user_svc.get_user_state("alice")["bw_used"]), 0)
            self.assertEqual(int(subscription.user_svc.get_user_state("alice")["wl_used"]), 0)
            database.close()

    def test_daily_snapshot_accumulates_repeated_days(self) -> None:
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
            readings = [
                BandwidthInfo(upload=100, download=200, total=300),
                BandwidthInfo(upload=400, download=700, total=1100),
            ]
            calls = 0

            def bandwidth(username: str, whitelist: bool = False) -> BandwidthInfo:
                nonlocal calls
                if whitelist:
                    return readings[min(calls, len(readings) - 1)]
                current = readings[min(calls, len(readings) - 1)]
                calls += 1
                return current

            watch.mem["alice"] = BandwidthInfo(upload=0, download=0, total=0)
            watch.wl_mem["alice"] = BandwidthInfo(upload=0, download=0, total=0)
            subscription.bandwidth_svc.bandwidth = bandwidth  # type: ignore[method-assign]
            watch.record_daily_snapshot()
            watch.record_daily_snapshot()

            row = database.get_bandwidth_snapshots("alice", 0)[0]
            self.assertEqual(
                (row["up"], row["down"], row["wl_up"], row["wl_down"]),
                (400, 700, 400, 700),
            )
            database.close()

    def test_daily_snapshot_clamps_counter_reset_deltas(self) -> None:
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
            baseline = BandwidthInfo(upload=1000, download=2000, total=3000)
            reset = BandwidthInfo(upload=100, download=200, total=300)

            def bandwidth(username: str, whitelist: bool = False) -> BandwidthInfo:
                return reset

            watch.mem["alice"] = baseline
            watch.wl_mem["alice"] = baseline
            subscription.bandwidth_svc.bandwidth = bandwidth  # type: ignore[method-assign]
            watch.record_daily_snapshot()
            rows = database.get_bandwidth_snapshots("alice", 0)

            self.assertEqual(len(rows), 1)
            self.assertEqual(
                (rows[0]["up"], rows[0]["down"], rows[0]["wl_up"], rows[0]["wl_down"]),
                (0, 0, 0, 0),
            )
            database.close()

if __name__ == "__main__":
    unittest.main()
