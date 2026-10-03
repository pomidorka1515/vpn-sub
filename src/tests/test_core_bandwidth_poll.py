from __future__ import annotations

import time
import sqlite3
from collections.abc import Callable

from bwatch import BWatch
from core import Subscription
from custom_types import BandwidthInfo
from db import Database
from errors import PanelRejectedError
from helpers import create_alice, make_watch


def _count_queries(
    database: Database,
) -> tuple[list[str], Callable[[], sqlite3.Connection]]:
    seen: list[str] = []
    original = database._connect

    def traced() -> sqlite3.Connection:
        conn = original()
        conn.set_trace_callback(seen.append)
        return conn

    database._connect = traced  # type: ignore[method-assign]
    return seen, original


def test_periodic_loop_guard_swallows_crashes(
    database: Database, subscription: Subscription, watch: BWatch,
) -> None:
    def boom() -> None:
        raise RuntimeError("poll exploded")

    # a crashing operation must not kill the periodic loop thread
    watch._guarded("bandwidth poll", boom)  # pyright: ignore[reportPrivateUsage]


class _RecordingBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int | str | None, str]] = []

    def msg(self, tgid: int | str | None, key: str, **kwargs: object) -> None:
        self.sent.append((tgid, key))


def test_no_traffic_disabled_notification_when_panel_update_fails(
    database: Database, subscription: Subscription,
) -> None:
    bot = _RecordingBot()
    failing_watch = make_watch(database, subscription, bot=bot)
    create_alice(database, bw_limit_gb=1, wl_limit_gb=0)
    database.update_user("alice", bw_used=2 * 10**9)

    def fail_update(*args: object, **kwargs: object) -> None:
        raise PanelRejectedError("panel rejected")

    subscription.business_svc.update_user = fail_update  # type: ignore[method-assign]
    failing_watch.check()

    # the disable never happened; the user must not be told it did
    assert bot.sent == []


def test_bonus_on_zero_quota_reenables_on_next_poll(
    database: Database, subscription: Subscription, watch: BWatch,
) -> None:
    create_alice(database, bw_limit_gb=0, wl_limit_gb=0, expires_at=0)
    database.update_user("alice", status=False, status_time=False, status_wl=False, bw_used=5 * 10**9)
    subscription.code_svc.add_code("bonus1", "bonus", gb=3, wl_gb=1, uses=1)
    subscription.code_svc.apply_bonus_code(username="alice", code="bonus1")

    enabled: list[tuple[str, bool | None, bool | None]] = []

    def record_update(
        username: str,
        enable: bool | None = None,
        timee: bool | None = None,
        wl_enable: bool | None = None,
    ) -> bool:
        enabled.append((username, enable, wl_enable))
        fields: dict[str, bool] = {}
        if enable is not None:
            fields["status"] = enable
        if timee is not None:
            fields["status_time"] = timee
        if wl_enable is not None:
            fields["status_wl"] = wl_enable
        database.update_user(username, **fields)
        return True

    watch._update_user = record_update  # type: ignore[method-assign]
    watch.bandwidth_check()

    record = database.get_user("alice")
    assert record is not None
    assert ("alice", True, None) in enabled
    assert ("alice", None, True) in enabled
    assert (record["enabled"], record["enabled_time"], record["enabled_wl"]) == (1, 1, 1)


def test_lapsed_bonus_renewal_reenables_time(
    database: Database, subscription: Subscription, watch: BWatch,
) -> None:
    create_alice(database, bw_limit_gb=1, wl_limit_gb=0, expires_at=1)
    database.update_user("alice", status=False, status_time=False)
    subscription.code_svc.add_code("bonus1", "bonus", days=7, uses=1)
    subscription.code_svc.apply_bonus_code(username="alice", code="bonus1")

    def record_update(
        username: str,
        enable: bool | None = None,
        timee: bool | None = None,
        wl_enable: bool | None = None,
    ) -> bool:
        fields: dict[str, bool] = {}
        if enable is not None:
            fields["status"] = enable
        if timee is not None:
            fields["status_time"] = timee
        if wl_enable is not None:
            fields["status_wl"] = wl_enable
        database.update_user(username, **fields)
        return True

    watch._update_user = record_update  # type: ignore[method-assign]
    watch.bandwidth_check()

    record = database.get_user("alice")
    assert record is not None
    assert record["expires_at"] > int(time.time())
    assert (record["enabled"], record["enabled_time"]) == (1, 1)


def test_no_expiry_disabled_notification_when_panel_update_fails(
    database: Database, subscription: Subscription,
) -> None:
    bot = _RecordingBot()
    failing_watch = make_watch(database, subscription, bot=bot)
    create_alice(database, bw_limit_gb=0, expires_at=1)

    def fail_update(*args: object, **kwargs: object) -> None:
        raise PanelRejectedError("panel rejected")

    subscription.business_svc.update_user = fail_update  # type: ignore[method-assign]
    failing_watch.check()

    assert bot.sent == []


def test_counter_failure_does_not_advance_either_baseline(
    database: Database, subscription: Subscription, watch: BWatch,
) -> None:
    create_alice(database, bw_limit_gb=1, wl_limit_gb=1)
    first = BandwidthInfo(upload=100, download=100, total=200)
    second = BandwidthInfo(upload=200, download=200, total=400)
    calls: list[bool] = []

    def all_traffic(whitelist: bool = False, *, pool: object = None) -> dict[str, BandwidthInfo]:
        calls.append(whitelist)
        if whitelist:
            raise RuntimeError("whitelist panel failed")
        return {"alice": second}

    watch.mem["alice"] = first
    watch.wl_mem["alice"] = first
    subscription.bandwidth_svc.all_traffic = all_traffic  # type: ignore[method-assign]
    watch.bandwidth_check()

    # both maps are read before either baseline advances
    assert calls == [False, True]
    assert watch.mem["alice"] == first
    assert watch.wl_mem["alice"] == first
    assert int(subscription.user_svc.get_user_state("alice")["bw_used"]) == 0
    assert int(subscription.user_svc.get_user_state("alice")["wl_used"]) == 0


def test_bandwidth_check_reads_one_map_per_side(
    database: Database, subscription: Subscription, watch: BWatch,
) -> None:
    create_alice(database, bw_limit_gb=1, wl_limit_gb=1)
    calls: list[bool] = []

    def all_traffic(whitelist: bool = False, *, pool: object = None) -> dict[str, BandwidthInfo]:
        calls.append(whitelist)
        return {"alice": BandwidthInfo(400, 400, 800)} if not whitelist else {}

    watch.mem["alice"] = BandwidthInfo(100, 100, 200)
    watch.wl_mem["alice"] = BandwidthInfo(100, 100, 200)
    subscription.bandwidth_svc.all_traffic = all_traffic  # type: ignore[method-assign]
    watch.bandwidth_check()

    # exactly two batched reads per cycle: main + whitelist
    assert calls == [False, True]
    assert int(subscription.user_svc.get_user_state("alice")["bw_used"]) == 600
    assert watch.mem["alice"].total == 800


def test_bandwidth_check_skips_unrequired_side(
    database: Database, subscription: Subscription, watch: BWatch,
) -> None:
    create_alice(database, bw_limit_gb=1, wl_limit_gb=0)
    calls: list[bool] = []

    def all_traffic(whitelist: bool = False, *, pool: object = None) -> dict[str, BandwidthInfo]:
        calls.append(whitelist)
        return {"alice": BandwidthInfo(300, 300, 600)}

    watch.mem["alice"] = BandwidthInfo(0, 0, 0)
    subscription.bandwidth_svc.all_traffic = all_traffic  # type: ignore[method-assign]
    watch.bandwidth_check()

    assert calls == [False]
    assert int(subscription.user_svc.get_user_state("alice")["bw_used"]) == 600


def test_daily_snapshot_accumulates_repeated_days(
    database: Database, subscription: Subscription, watch: BWatch,
) -> None:
    create_alice(database, bw_limit_gb=1, wl_limit_gb=1)
    readings = [
        BandwidthInfo(upload=100, download=200, total=300),
        BandwidthInfo(upload=400, download=700, total=1100),
    ]
    calls = 0

    def all_traffic(whitelist: bool = False, *, pool: object = None) -> dict[str, BandwidthInfo]:
        nonlocal calls
        current = readings[min(calls, len(readings) - 1)]
        if not whitelist:
            calls += 1
        return {"alice": current}

    watch.snap_mem["alice"] = BandwidthInfo(upload=0, download=0, total=0)
    watch.snap_wl_mem["alice"] = BandwidthInfo(upload=0, download=0, total=0)
    subscription.bandwidth_svc.all_traffic = all_traffic  # type: ignore[method-assign]
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

    def all_traffic(whitelist: bool = False, *, pool: object = None) -> dict[str, BandwidthInfo]:
        return {"alice": reset}

    watch.snap_mem["alice"] = baseline
    watch.snap_wl_mem["alice"] = baseline
    subscription.bandwidth_svc.all_traffic = all_traffic  # type: ignore[method-assign]
    watch.record_daily_snapshot()
    rows = database.get_bandwidth_snapshots("alice", 0)

    assert len(rows) == 1
    assert (rows[0]["up"], rows[0]["down"], rows[0]["wl_up"], rows[0]["wl_down"]) == (0, 0, 0, 0)
    assert watch.snap_mem["alice"] == baseline
    assert watch.snap_wl_mem["alice"] == baseline


def test_daily_snapshot_ignores_poller_baseline(
    database: Database, subscription: Subscription, watch: BWatch,
) -> None:
    create_alice(database, bw_limit_gb=1, wl_limit_gb=1)
    current = BandwidthInfo(upload=400, download=600, total=1000)
    poller = BandwidthInfo(upload=399, download=599, total=998)

    def all_traffic(whitelist: bool = False, *, pool: object = None) -> dict[str, BandwidthInfo]:
        return {"alice": current}

    watch.mem["alice"] = poller
    watch.wl_mem["alice"] = poller
    watch.snap_mem["alice"] = BandwidthInfo(upload=0, download=0, total=0)
    watch.snap_wl_mem["alice"] = BandwidthInfo(upload=0, download=0, total=0)
    subscription.bandwidth_svc.all_traffic = all_traffic  # type: ignore[method-assign]
    watch.record_daily_snapshot()

    row = database.get_bandwidth_snapshots("alice", 0)[0]
    assert (row["up"], row["down"], row["up"] + row["down"], row["wl_up"], row["wl_down"]) == (
        400, 600, 1000, 400, 600,
    )
    assert watch.mem["alice"] == poller
    assert watch.wl_mem["alice"] == poller


def test_daily_snapshot_skips_unused_counter(
    database: Database, subscription: Subscription, watch: BWatch,
) -> None:
    create_alice(database, bw_limit_gb=1, wl_limit_gb=0)
    calls: list[bool] = []

    def all_traffic(whitelist: bool = False, *, pool: object = None) -> dict[str, BandwidthInfo]:
        calls.append(whitelist)
        if whitelist:
            raise RuntimeError("whitelist panel failed")
        return {"alice": BandwidthInfo(upload=10, download=20, total=30)}

    watch.snap_mem["alice"] = BandwidthInfo(upload=0, download=0, total=0)
    subscription.bandwidth_svc.all_traffic = all_traffic  # type: ignore[method-assign]
    watch.record_daily_snapshot()

    assert calls == [False]
    row = database.get_bandwidth_snapshots("alice", 0)[0]
    assert (row["up"], row["down"], row["wl_up"], row["wl_down"]) == (10, 20, 0, 0)


def test_bandwidth_check_reads_users_once_and_writes_once(
    database: Database, subscription: Subscription, watch: BWatch,
) -> None:
    create_alice(database, bw_limit_gb=1, wl_limit_gb=1)
    database.create_user(
        username="bob", uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        token="b" * 40, fingerprint="chrome", displayname="Bob",
        bw_limit_gb=1, wl_limit_gb=0,
    )
    watch.mem["alice"] = BandwidthInfo(0, 0, 0)
    watch.wl_mem["alice"] = BandwidthInfo(0, 0, 0)
    watch.mem["bob"] = BandwidthInfo(0, 0, 0)

    def all_traffic(whitelist: bool = False, *, pool: object = None) -> dict[str, BandwidthInfo]:
        total = 20 if whitelist else 10
        return {"alice": BandwidthInfo(total, 0, total), "bob": BandwidthInfo(5, 0, 5)}

    subscription.bandwidth_svc.all_traffic = all_traffic  # type: ignore[method-assign]
    seen, original = _count_queries(database)
    try:
        watch.bandwidth_check()
    finally:
        database._connect = original  # type: ignore[method-assign]

    selects = [q for q in seen if q.startswith("SELECT")]
    updates = [q for q in seen if q.startswith("UPDATE users SET bw_used")]
    assert len(selects) == 1
    assert len(updates) == 2
    assert seen.count("BEGIN IMMEDIATE") == 1
    assert int(database.get_user("alice")["bw_used"]) == 10  # type: ignore[index]
    assert int(database.get_user("alice")["wl_used"]) == 20  # type: ignore[index]
    assert int(database.get_user("bob")["bw_used"]) == 5  # type: ignore[index]


def test_check_reads_users_and_telegram_once(
    database: Database, subscription: Subscription, watch: BWatch,
) -> None:
    create_alice(database, bw_limit_gb=0, wl_limit_gb=0)
    database.create_user(
        username="bob", uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        token="b" * 40, fingerprint="chrome", displayname="Bob",
    )
    seen, original = _count_queries(database)
    try:
        watch.check()
    finally:
        database._connect = original  # type: ignore[method-assign]

    selects = [q for q in seen if q.startswith("SELECT")]
    assert len(selects) == 2
    assert any("FROM users" in q for q in selects)
    assert any("FROM telegram_mappings" in q for q in selects)


def test_daily_snapshot_writes_rows_in_one_transaction(
    database: Database, subscription: Subscription, watch: BWatch,
) -> None:
    create_alice(database, bw_limit_gb=1, wl_limit_gb=0)
    database.create_user(
        username="bob", uuid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        token="b" * 40, fingerprint="chrome", displayname="Bob",
        bw_limit_gb=1, wl_limit_gb=0,
    )

    def all_traffic(whitelist: bool = False, *, pool: object = None) -> dict[str, BandwidthInfo]:
        return {
            "alice": BandwidthInfo(upload=1, download=2, total=3),
            "bob": BandwidthInfo(upload=4, download=5, total=9),
        }

    subscription.bandwidth_svc.all_traffic = all_traffic  # type: ignore[method-assign]
    seen, original = _count_queries(database)
    try:
        watch.record_daily_snapshot()
    finally:
        database._connect = original  # type: ignore[method-assign]

    user_selects = [q for q in seen if "FROM users" in q]
    assert len(user_selects) == 1
    insert_at = next(
        i for i, q in enumerate(seen) if q.startswith("INSERT INTO bandwidth_snapshots")
    )
    # both rows share the snapshot transaction; prune/metadata are later
    assert seen[insert_at - 1] == "BEGIN IMMEDIATE"
    assert seen[insert_at + 1].startswith("INSERT INTO bandwidth_snapshots")
    assert seen[insert_at + 2] == "COMMIT"
