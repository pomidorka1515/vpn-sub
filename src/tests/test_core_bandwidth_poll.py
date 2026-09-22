from __future__ import annotations

from bwatch import BWatch
from core import Subscription
from custom_types import BandwidthInfo
from db import Database
from helpers import create_alice


def test_partial_counter_failure_does_not_advance_either_baseline(
    database: Database, subscription: Subscription, watch: BWatch,
) -> None:
    create_alice(database, bw_limit_gb=1, wl_limit_gb=1)
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

    assert calls == [False, True]
    assert watch.mem["alice"] == first
    assert watch.wl_mem["alice"] == first
    assert int(subscription.user_svc.get_user_state("alice")["bw_used"]) == 0
    assert int(subscription.user_svc.get_user_state("alice")["wl_used"]) == 0


def test_daily_snapshot_accumulates_repeated_days(
    database: Database, subscription: Subscription, watch: BWatch,
) -> None:
    create_alice(database, bw_limit_gb=1, wl_limit_gb=1)
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
    assert (row["up"], row["down"], row["wl_up"], row["wl_down"]) == (400, 700, 400, 700)


def test_daily_snapshot_clamps_counter_reset_deltas(
    database: Database, subscription: Subscription, watch: BWatch,
) -> None:
    create_alice(database, bw_limit_gb=1, wl_limit_gb=1)
    baseline = BandwidthInfo(upload=1000, download=2000, total=3000)
    reset = BandwidthInfo(upload=100, download=200, total=300)

    def bandwidth(username: str, whitelist: bool = False) -> BandwidthInfo:
        return reset

    watch.mem["alice"] = baseline
    watch.wl_mem["alice"] = baseline
    subscription.bandwidth_svc.bandwidth = bandwidth  # type: ignore[method-assign]
    watch.record_daily_snapshot()
    rows = database.get_bandwidth_snapshots("alice", 0)

    assert len(rows) == 1
    assert (rows[0]["up"], rows[0]["down"], rows[0]["wl_up"], rows[0]["wl_down"]) == (0, 0, 0, 0)
