from __future__ import annotations

import uuid

import pytest

from bwatch import BWatch
from core import Subscription
from custom_types import BandwidthInfo
from db import Database
from errors import PanelUnavailableError
from helpers import create_alice


def test_daily_snapshot_retries_only_failed_kind(watch: BWatch) -> None:
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
    assert (len(bandwidth_calls), len(state_calls)) == (1, 1)

    watch._snapshot_due_at = {"bandwidth": 3600.0, "state": 86400.0}  # pyright: ignore[reportPrivateUsage]
    watch._run_due_daily_snapshots(3600.0)  # pyright: ignore[reportPrivateUsage]
    assert (len(bandwidth_calls), len(state_calls)) == (2, 1)

    watch._snapshot_due_at = {"bandwidth": 7200.0, "state": 86400.0}  # pyright: ignore[reportPrivateUsage]
    watch._run_due_daily_snapshots(7200.0)  # pyright: ignore[reportPrivateUsage]
    assert (len(bandwidth_calls), len(state_calls)) == (3, 1)


def test_all_daily_snapshot_failures_raise_and_persist_counts(
    database: Database, subscription: Subscription, watch: BWatch,
) -> None:
    create_alice(database, bw_limit_gb=1, wl_limit_gb=1)

    def fail_traffic(whitelist: bool = False) -> dict[str, BandwidthInfo]:
        raise RuntimeError("panel unavailable")

    subscription.bandwidth_svc.all_traffic = fail_traffic  # type: ignore[method-assign]
    with pytest.raises(PanelUnavailableError):
        watch.record_daily_snapshot()
    failure = watch.get_daily_snapshot_failure()
    assert failure is not None
    assert (failure["failed"], failure["eligible"]) == (1, 1)


def test_successful_daily_snapshot_clears_previous_failure(
    database: Database, subscription: Subscription, watch: BWatch,
) -> None:
    create_alice(database, uuid=str(uuid.uuid4()), bw_limit_gb=1, wl_limit_gb=1)
    database.set_metadata("daily_bw_snapshot_failures", "123:1:1")

    def all_traffic(whitelist: bool = False) -> dict[str, BandwidthInfo]:
        return {"alice": BandwidthInfo(1, 2, 3)}

    subscription.bandwidth_svc.all_traffic = all_traffic  # type: ignore[method-assign]
    watch.record_daily_snapshot()
    assert watch.get_daily_snapshot_failure() is None


def test_partial_daily_snapshot_failure_is_visible(
    database: Database, subscription: Subscription, watch: BWatch,
) -> None:
    for username in ("alice", "bob"):
        database.create_user(
            username=username, uuid=str(uuid.uuid4()),
            token=username.ljust(40, "x"), fingerprint="chrome",
            displayname=username.title(), bw_limit_gb=1, wl_limit_gb=1,
        )

    def all_traffic(whitelist: bool = False) -> dict[str, BandwidthInfo]:
        if whitelist:
            raise RuntimeError("panel unavailable")
        return {"bob": BandwidthInfo(1, 2, 3)}

    subscription.bandwidth_svc.all_traffic = all_traffic  # type: ignore[method-assign]
    with pytest.raises(PanelUnavailableError):
        watch.record_daily_snapshot()
    failure = watch.get_daily_snapshot_failure()
    assert failure is not None
    assert (failure["failed"], failure["eligible"]) == (2, 2)


def test_partial_daily_snapshot_failure_does_not_advance_failed_baseline(
    database: Database, subscription: Subscription, watch: BWatch,
) -> None:
    for username in ("alice", "bob"):
        database.create_user(
            username=username, uuid=str(uuid.uuid4()),
            token=username.ljust(40, "x"), fingerprint="chrome",
            displayname=username.title(), bw_limit_gb=1, wl_limit_gb=1,
        )

    zero = BandwidthInfo(upload=0, download=0, total=0)
    watch.snap_mem["alice"] = zero
    watch.snap_wl_mem["alice"] = zero
    watch.snap_mem["bob"] = zero
    watch.snap_wl_mem["bob"] = zero

    readings = {
        "alice": BandwidthInfo(upload=100, download=200, total=300),
        "bob": BandwidthInfo(upload=10, download=20, total=30),
    }

    def all_traffic(whitelist: bool = False) -> dict[str, BandwidthInfo]:
        if whitelist:
            raise RuntimeError("panel unavailable")
        return readings

    subscription.bandwidth_svc.all_traffic = all_traffic  # type: ignore[method-assign]
    with pytest.raises(PanelUnavailableError):
        watch.record_daily_snapshot()

    # No rows were written, no baseline advanced.
    assert database.get_bandwidth_snapshots("alice", 0) == []
    assert database.get_bandwidth_snapshots("bob", 0) == []
    assert watch.snap_mem["alice"] == zero
    assert watch.snap_wl_mem["alice"] == zero
    assert watch.get_daily_snapshot_failure() is not None
