from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from bwatch.watch import BWatch
from custom_types import BandwidthInfo
from db import Database
from errors import PanelUnavailableError
from tests.support.panels import FakePanel, make_panel_client
from tests.support.subscriptions import close_subscription, make_subscription
from tests.support.users import USER_UUID

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from db.state import UsageBaselineKey, UsageBaselines


@pytest.mark.asyncio
async def test_usage_reads_only_metered_or_recovery_users(database: Database) -> None:
    for name in ("metered", "unlimited", "disabled", "pending"):
        await database.create_user(
            username=name,
            uuid=f"{name}-uuid",
            token=name * 8,
            fingerprint="chrome",
            displayname=name,
            bw_limit_gb=int(name == "metered"),
        )
    await database.update_user("disabled", status=False)
    await database.set_metadata("user_sync_pending:pending", "{}")
    states = await database.list_sweep_users(active=True)
    assert {state["username"] for state in states} == {"metered", "disabled", "pending"}
    assert len(await database.list_sweep_users()) == 4
    baselines = {
        ("main", "panel", name, f"{name}-uuid"): BandwidthInfo(100, 0, 100)
        for name in ("metered", "unlimited")
    }
    await database.increment_usages({}, baselines=baselines)
    assert (await database.load_usage_baselines({"metered"}))[1] == {
        key: value for key, value in baselines.items() if key[2] == "metered"
    }
    assert (await database.load_usage_baselines(set()))[1] == {}
    assert (await database.load_usage_baselines())[1] == baselines
    await database.increment_usages(
        {}, baselines={("main", "panel", "unlimited", "unlimited-uuid"): BandwidthInfo(1, 0, 1)}
    )
    # Previously metered users must be visited once more to discard old counters.
    assert "unlimited" in {
        state["username"] for state in await database.list_sweep_users(active=True)
    }
    async with database.connection() as conn:
        plans = await conn.rows(
            "EXPLAIN QUERY PLAN SELECT username FROM users WHERE "
            "bw_limit_gb > 0 OR wl_limit_gb > 0 "
            "OR enabled = 0 OR enabled_time = 0 OR enabled_wl = 0 "
            "OR admin_disabled = 1 OR admin_disabled_wl = 1"
        )
    assert any("idx_users_usage_sweep" in str(row[3]) for row in plans)


@pytest.mark.asyncio
async def test_monthly_reset_catches_up_once_after_outage(
    database: Database,
    watch: BWatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> Clock:
            return cls(2026, 10, 3, tzinfo=UTC)

    monkeypatch.setattr("bwatch.usage.datetime", Clock)
    await database.create_user(
        username="alice",
        uuid=USER_UUID,
        token="a" * 40,
        fingerprint="chrome",
        displayname="Alice",
        bw_limit_gb=10,
        wl_limit_gb=10,
    )
    await database.reset_monthly("2026-09", "2026-09-01")
    await database.increment_usage("alice", regular=100, whitelist=200)

    await watch.is_first()
    state = await database.get_user("alice")
    assert state is not None
    assert (state["bw_used"], state["wl_used"]) == (0, 0)
    assert await database.get_metadata("last_reset_month") == "2026-10"
    assert await database.get_metadata("last_reset") == "2026-10-03"

    await database.increment_usage("alice", regular=30, whitelist=40)
    await watch.is_first()
    state = await database.get_user("alice")
    assert state is not None
    assert (state["bw_used"], state["wl_used"]) == (30, 40)


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("missing_panel", [False, True])
async def test_month_rollover_rebaselines_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restart: bool,
    missing_panel: bool,
) -> None:
    now = datetime(2026, 9, 30, 23, 59, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> Clock:
            return cls(now.year, now.month, now.day, now.hour, now.minute, tzinfo=UTC)

    monkeypatch.setattr("bwatch.usage.datetime", Clock)
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    total = 100
    missing = False

    async def traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
        require_complete: bool = False,
        known: Mapping[str, str] | None = None,
    ) -> dict[str, dict[str, BandwidthInfo]]:
        return {} if missing else {"panel": {"alice": BandwidthInfo(total, 0, total)}}

    async def no_snapshots(now: float) -> None:
        return None

    sub.bandwidth_svc.panel_traffic = traffic  # type: ignore[method-assign]
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1,
            wl_limit_gb=1,
        )
        await watch.bandwidth_check()
        total = 140
        await watch.bandwidth_check()
        assert (await db.get_user("alice"))["bw_used"] == 40  # type: ignore[index]
        now = datetime(2026, 10, 3 if restart else 1, tzinfo=UTC)
        total = 900  # Unknown mix of September and October traffic.
        missing = missing_panel
        if restart:
            await watch.close()
            watch = BWatch(sub.res.cfg, db, sub, scheduled=True)
            watch._run_due_daily_snapshots = no_snapshots  # type: ignore[method-assign]
            await watch.start()
        else:
            await watch.bandwidth_check()
        state = await db.get_user("alice")
        assert state is not None
        assert (state["bw_used"], state["wl_used"]) == (0, 0)
        assert await db.get_metadata("last_reset_month") == "2026-10"
        missing = False
        await watch.bandwidth_check()  # Returning panels also need a fresh baseline.
        total = 925
        await watch.bandwidth_check()
        await watch.is_first()  # A later date check must not erase October usage.
        state = await db.get_user("alice")
        assert state is not None
        assert (state["bw_used"], state["wl_used"]) == (25, 25)
    finally:
        await watch.close()
        await close_subscription(sub)
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outage", ["one", "main", "whitelist", "all"])
async def test_panel_outage_keeps_healthy_metering_and_rollover_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outage: str,
) -> None:
    now = datetime(2026, 9, 30, 23, 59, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> Clock:
            return cls(now.year, now.month, now.day, now.hour, now.minute, tzinfo=UTC)

    monkeypatch.setattr("bwatch.usage.datetime", Clock)
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    panels = [FakePanel(name=name) for name in ("one", "two", "whitelist")]
    sub = await make_subscription(db, tmp_path, panels=panels[:2], whitelist_panel=panels[2])
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    recovery = AsyncMock()
    watch._quota = recovery  # type: ignore[method-assign]
    sample_clock = 0.0
    monkeypatch.setattr("core.services.panel.monotonic", lambda: sample_clock)

    async def sample() -> None:
        nonlocal sample_clock
        sample_clock += 15
        await watch.bandwidth_check()

    def readings(total: int) -> None:
        for panel in panels:
            panel.clients = [make_panel_client("alice", [1], up=total, down=0)]

    failed = {
        "one": {"one"},
        "main": {"one", "two"},
        "whitelist": {"whitelist"},
        "all": {"one", "two", "whitelist"},
    }[outage]
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1,
            wl_limit_gb=1,
        )
        readings(100)
        await sample()
        readings(140)
        for panel in panels:
            if panel.name in failed:
                panel._get_error = RuntimeError("offline")
        # Keep a real recovery candidate so outages cannot hide recovery work.
        await db.update_user("alice", status=False)
        recovery.reset_mock()
        await sample()
        recovery.assert_awaited_once_with("alice", recover=True)
        expected = (40 * len({"one", "two"} - failed), 0 if "whitelist" in failed else 40)
        state = await db.get_user("alice")
        assert state is not None
        assert (state["bw_used"], state["wl_used"]) == expected
        for panel in panels:
            baseline = watch.wl_mem if panel.name == "whitelist" else watch.mem
            assert baseline[panel.name, "alice", USER_UUID].total == (
                100 if panel.name in failed else 140
            )

        # An outage must preserve durable baselines across watcher restarts too.
        await watch.close()
        watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
        watch._quota = recovery  # type: ignore[method-assign]
        await sample()
        for panel in panels:
            panel._get_error = None
        readings(160)
        await sample()
        state = await db.get_user("alice")
        assert state is not None
        assert (state["bw_used"], state["wl_used"]) == (120, 60)

        # Reintroduce the outage across midnight: rollover cannot wait for it.
        for panel in panels:
            if panel.name in failed:
                panel._get_error = RuntimeError("offline")
        now = datetime(2026, 10, 1, tzinfo=UTC)
        readings(900)
        recovery.reset_mock()
        await sample()
        recovery.assert_awaited_once_with("alice", recover=True)
        state = await db.get_user("alice")
        assert state is not None
        assert (state["bw_used"], state["wl_used"]) == (0, 0)
        assert await db.get_metadata("last_reset_month") == "2026-10"
        for panel in panels:
            panel._get_error = None
        await sample()  # Returning panels need new-month baselines.
        readings(925)
        await sample()
        state = await db.get_user("alice")
        assert state is not None
        assert (state["bw_used"], state["wl_used"]) == (50, 25)
    finally:
        await watch.close()
        await close_subscription(sub)
        await db.close()


@pytest.mark.asyncio
async def test_accounting_persists_before_advancing_baselines(tmp_path: Path) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=10,
            wl_limit_gb=0,
        )
        await db.increment_usages(
            {}, baselines={("main", "panel", "alice", USER_UUID): BandwidthInfo(10, 20, 30)}
        )
        await watch.start()

        async def traffic(
            whitelist: bool = False,
            *,
            work: str = "foreground",
        ) -> dict[str, BandwidthInfo]:
            return {"alice": BandwidthInfo(20, 40, 60)}

        sub.bandwidth_svc.all_traffic = traffic  # type: ignore[method-assign]

        async def panel_traffic(
            whitelist: bool = False,
            *,
            work: str = "foreground",
            require_complete: bool = False,
            known: Mapping[str, str] | None = None,
        ) -> dict[str, dict[str, BandwidthInfo]]:
            return {"panel": await traffic(whitelist, work=work)}

        sub.bandwidth_svc.panel_traffic = panel_traffic  # type: ignore[method-assign]
        increment = db.increment_usages

        async def failed_commit(
            updates: object,
            *,
            baselines: UsageBaselines | None = None,
            removed_baselines: set[UsageBaselineKey] | None = None,
            clear_baselines: bool = False,
            period: tuple[str, str] | None = None,
        ) -> None:
            raise RuntimeError("disk unavailable")

        db.increment_usages = failed_commit  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="disk unavailable"):
            await watch.bandwidth_check()
        assert watch.mem["panel", "alice", USER_UUID].total == 30
        db.increment_usages = increment  # type: ignore[method-assign]
        await watch.bandwidth_check()
        await watch.bandwidth_check()
        assert (await db.get_user("alice"))["bw_used"] == 30  # type: ignore[index]
        watch.snap_mem["panel", "alice", USER_UUID] = BandwidthInfo(10, 20, 30)
        await watch.record_daily_snapshot()
        rows = await db.get_bandwidth_snapshots("alice", 0)
        assert rows[0]["up"] == 10
        assert rows[0]["down"] == 20
    finally:
        await watch.close()
        await close_subscription(sub)
        await db.close()


@pytest.mark.asyncio
async def test_counter_reset_confirmation_and_restart(tmp_path: Path) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    restarted = BWatch(sub.res.cfg, db, sub, scheduled=False)
    totals: dict[str, int | None] = {"main": 100, "whitelist": 200}

    async def traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
        require_complete: bool = False,
        known: Mapping[str, str] | None = None,
    ) -> dict[str, dict[str, BandwidthInfo]]:
        total = totals["whitelist" if whitelist else "main"]
        return {"panel": {} if total is None else {"alice": BandwidthInfo(total, 0, total)}}

    sub.bandwidth_svc.panel_traffic = traffic  # type: ignore[method-assign]
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1,
            wl_limit_gb=1,
        )
        await watch.start()
        await watch.bandwidth_check()
        totals.update(main=10, whitelist=20)
        await watch.bandwidth_check()
        totals.update(main=110, whitelist=210)
        await watch.bandwidth_check()  # A single low sample was a flake.
        totals.update(main=5, whitelist=7)
        await watch.bandwidth_check()
        await watch.close()
        await restarted.start()
        totals.update(main=8, whitelist=11)
        await restarted.bandwidth_check()
        totals.update(main=12, whitelist=16)
        await restarted.bandwidth_check()
        state = await db.get_user("alice")
        assert state is not None
        assert (state["bw_used"], state["wl_used"]) == (22, 26)
        totals.update(main=None, whitelist=None)
        await restarted.bandwidth_check()
        assert restarted.mem["panel", "alice", USER_UUID].total == 12
        assert restarted.wl_mem["panel", "alice", USER_UUID].total == 16
    finally:
        await watch.close()
        await restarted.close()
        await close_subscription(sub)
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("readings", "expected"),
    [
        ([(100, 100), (10, 250), (12, 300)], 212),
        ([(100, 100), (0, 100), (0, 100), (4, 105)], 9),
        ([(100, 100), (10, 100), (None, 100), (12, 100), (14, 100)], 14),
        ([(100, 100), (10, 250), (110, 300)], 210),
        ([(100, 100), (10, 100), (5, 100), (8, 100)], 8),
    ],
)
async def test_panel_resets_account_full_confirmed_traffic(
    tmp_path: Path,
    readings: list[tuple[int | None, int]],
    expected: int,
) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    totals: tuple[int | None, int] = readings[0]

    async def traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
        require_complete: bool = False,
        known: Mapping[str, str] | None = None,
    ) -> dict[str, dict[str, BandwidthInfo]]:
        return {
            panel: {} if total is None else {"alice": BandwidthInfo(total, 0, total)}
            for panel, total in zip(("one", "two"), totals, strict=True)
        }

    sub.bandwidth_svc.panel_traffic = traffic  # type: ignore[method-assign]
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1,
            wl_limit_gb=1,
        )
        await watch.start()
        for reading in readings:
            totals = reading
            await watch.bandwidth_check()
        await watch.bandwidth_check()  # Committed traffic is not counted twice.
        state = await db.get_user("alice")
        assert state is not None
        assert (state["bw_used"], state["wl_used"]) == (expected, expected)
    finally:
        await watch.close()
        await close_subscription(sub)
        await db.close()


@pytest.mark.asyncio
async def test_usage_baselines_do_not_cross_account_identities(tmp_path: Path) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    total = 100

    async def traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
        require_complete: bool = False,
        known: Mapping[str, str] | None = None,
    ) -> dict[str, dict[str, BandwidthInfo]]:
        return {"panel": {"alice": BandwidthInfo(total, 0, total)}}

    sub.bandwidth_svc.panel_traffic = traffic  # type: ignore[method-assign]
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1,
            wl_limit_gb=1,
        )
        await watch.start()
        await watch.bandwidth_check()
        total = 10
        await watch.bandwidth_check()  # Old identity has a pending reset.
        await db.update_user("alice", uuid="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        total = 12
        await watch.bandwidth_check()  # New identity starts a fresh baseline.
        total = 15
        await watch.bandwidth_check()
        state = await db.get_user("alice")
        assert state is not None
        assert (state["bw_used"], state["wl_used"]) == (3, 3)
        assert all(key[2] != USER_UUID for key in watch.mem.keys() | watch.wl_mem.keys())
    finally:
        await watch.close()
        await close_subscription(sub)
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_identity", [False, True])
async def test_usage_revalidates_identity_changed_during_panel_read(
    database: Database, monkeypatch: pytest.MonkeyPatch, restore_identity: bool
) -> None:
    await database.create_user(
        username="alice",
        uuid=USER_UUID,
        token="a" * 40,
        fingerprint="chrome",
        displayname="Alice",
        bw_limit_gb=1,
    )
    sub = await make_subscription(database)
    watch = BWatch(sub.res.cfg, database, sub, scheduled=False)
    total = 100
    changed = False

    async def traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
        require_complete: bool = False,
        known: Mapping[str, str] | None = None,
    ) -> dict[str, dict[str, BandwidthInfo]]:
        if changed:
            await database.update_user("alice", uuid="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
            if restore_identity:
                await database.update_user("alice", uuid=USER_UUID)
        return {"panel": {"alice": BandwidthInfo(total, 0, total)}}

    monkeypatch.setattr(sub.bandwidth_svc, "panel_traffic", traffic)
    try:
        await watch.bandwidth_check()
        total, changed = 200, True
        await watch.bandwidth_check()
        state = await database.get_user("alice")
        assert state is not None
        assert state["bw_used"] == 0
        _, saved = await database.load_usage_baselines()
        assert all(key[3] != USER_UUID for key in saved)
    finally:
        await watch.close()
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_usage_refreshes_metering_membership_without_full_scan(
    database: Database, watch: BWatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    await database.create_user(
        username="alice",
        uuid=USER_UUID,
        token="a" * 40,
        fingerprint="chrome",
        displayname="Alice",
        bw_limit_gb=1,
        wl_limit_gb=1,
    )
    traffic = AsyncMock(return_value={"panel": {"alice": BandwidthInfo(100, 0, 100)}})
    monkeypatch.setattr(watch.sub.bandwidth_svc, "panel_traffic", traffic)
    await watch.bandwidth_check()
    scans = AsyncMock(wraps=database.list_sweep_users)
    monkeypatch.setattr(database, "list_sweep_users", scans)
    await database.update_user("alice", bw_limit=0)
    await database.update_user("alice", bw_limit=1)
    traffic.return_value = {"panel": {"alice": BandwidthInfo(200, 0, 200)}}
    await watch.bandwidth_check()
    state = await database.get_user("alice")
    assert state is not None
    assert (state["bw_used"], state["wl_used"]) == (0, 100)
    await database.delete_user("alice")
    await watch.bandwidth_check()
    assert not watch.mem
    assert not watch.wl_mem
    scans.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [asyncio.CancelledError, RuntimeError])
async def test_usage_reloads_after_failed_commit_acknowledgement(
    database: Database,
    watch: BWatch,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    await database.create_user(
        username="alice",
        uuid=USER_UUID,
        token="a" * 40,
        fingerprint="chrome",
        displayname="Alice",
        bw_limit_gb=1,
    )
    traffic = AsyncMock(return_value={"panel": {"alice": BandwidthInfo(100, 0, 100)}})
    monkeypatch.setattr(watch.sub.bandwidth_svc, "panel_traffic", traffic)
    await watch.bandwidth_check()
    increment = database.increment_usages

    async def commit_then_fail(*args: object, **kwargs: object) -> None:
        await increment(*args, **kwargs)  # type: ignore[arg-type]
        raise failure

    traffic.return_value = {"panel": {"alice": BandwidthInfo(140, 0, 140)}}
    with monkeypatch.context() as patch:
        patch.setattr(database, "increment_usages", commit_then_fail)
        with pytest.raises(failure):
            await watch.bandwidth_check()
    await watch.bandwidth_check()
    traffic.return_value = {"panel": {"alice": BandwidthInfo(150, 0, 150)}}
    await watch.bandwidth_check()
    state = await database.get_user("alice")
    assert state is not None
    assert state["bw_used"] == 50


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [asyncio.CancelledError, RuntimeError])
async def test_usage_rebuilds_after_interrupted_dirty_refresh(
    database: Database,
    watch: BWatch,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    await database.create_user(
        username="alice",
        uuid=USER_UUID,
        token="a" * 40,
        fingerprint="chrome",
        displayname="Alice",
        bw_limit_gb=1,
    )
    traffic = AsyncMock(return_value={"panel": {"alice": BandwidthInfo(100, 0, 100)}})
    monkeypatch.setattr(watch.sub.bandwidth_svc, "panel_traffic", traffic)
    await watch.bandwidth_check()
    await database.update_user("alice", bw_limit=0)
    await database.update_user("alice", bw_limit=1)
    with monkeypatch.context() as patch:
        patch.setattr(database, "load_usage_baselines", AsyncMock(side_effect=failure))
        with pytest.raises(failure):
            await watch.bandwidth_check()
    # The invalidation has already been drained; a targeted retry would lose it.
    assert await database.take_usage_changes() == set()
    scans = AsyncMock(wraps=database.list_sweep_users)
    monkeypatch.setattr(database, "list_sweep_users", scans)
    traffic.return_value = {"panel": {"alice": BandwidthInfo(200, 0, 200)}}
    await watch.bandwidth_check()
    scans.assert_awaited_once_with(active=True)
    traffic.return_value = {"panel": {"alice": BandwidthInfo(230, 0, 230)}}
    await watch.bandwidth_check()
    state = await database.get_user("alice")
    assert state is not None
    assert state["bw_used"] == 30


@pytest.mark.asyncio
async def test_usage_only_writes_changed_baselines(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("alice", "bob"):
        await database.create_user(
            username=name,
            uuid=f"{name}-uuid",
            token=name * 8,
            fingerprint="chrome",
            displayname=name,
            bw_limit_gb=1,
        )
    sub = await make_subscription(database)
    watch = BWatch(sub.res.cfg, database, sub, scheduled=False)
    totals = {"alice": 100, "bob": 200}

    async def traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
        require_complete: bool = False,
        known: Mapping[str, str] | None = None,
    ) -> dict[str, dict[str, BandwidthInfo]]:
        return {"panel": {name: BandwidthInfo(total, 0, total) for name, total in totals.items()}}

    monkeypatch.setattr(sub.bandwidth_svc, "panel_traffic", traffic)
    try:
        await watch.bandwidth_check()
        async with database.transaction(immediate=True) as conn:
            await conn.execute("CREATE TABLE baseline_writes (username TEXT NOT NULL)")
            await conn.execute(
                "CREATE TRIGGER audit_baseline_update AFTER UPDATE ON usage_baselines "
                "BEGIN INSERT INTO baseline_writes VALUES (NEW.username); END"
            )
            await conn.execute(
                "CREATE TRIGGER audit_baseline_insert AFTER INSERT ON usage_baselines "
                "BEGIN INSERT INTO baseline_writes VALUES (NEW.username); END"
            )
        await watch.bandwidth_check()
        totals["alice"] = 120
        await watch.bandwidth_check()
        await watch.bandwidth_check()
        async with database.connection() as conn:
            rows = await conn.rows("SELECT username FROM baseline_writes")
        assert [row[0] for row in rows] == ["alice"]
        assert (await database.get_user("alice"))["bw_used"] == 20  # type: ignore[index]
    finally:
        await watch.close()
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_usage_commit_locks_only_users_with_deltas(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("alice", "bob"):
        await database.create_user(
            username=name,
            uuid=f"{name}-uuid",
            token=name * 8,
            fingerprint="chrome",
            displayname=name,
            bw_limit_gb=1,
        )
    sub = await make_subscription(database)
    watch = BWatch(sub.res.cfg, database, sub, scheduled=False)
    total = 100

    async def traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
        require_complete: bool = False,
        known: Mapping[str, str] | None = None,
    ) -> dict[str, dict[str, BandwidthInfo]]:
        return {"panel": {"alice": BandwidthInfo(total, 0, total)}}

    monkeypatch.setattr(sub.bandwidth_svc, "panel_traffic", traffic)
    entered, release = asyncio.Event(), asyncio.Event()
    increment = database.increment_usages

    async def delayed_increment(
        updates: Mapping[str, tuple[int, int]],
        *,
        baselines: UsageBaselines | None = None,
        removed_baselines: set[UsageBaselineKey] | None = None,
        clear_baselines: bool = False,
        period: tuple[str, str] | None = None,
    ) -> None:
        entered.set()
        await release.wait()
        await increment(
            updates,
            baselines=baselines,
            removed_baselines=removed_baselines,
            clear_baselines=clear_baselines,
            period=period,
        )

    try:
        await watch.bandwidth_check()
        total = 150
        monkeypatch.setattr(database, "increment_usages", delayed_increment)
        poll = asyncio.create_task(watch.bandwidth_check())
        await asyncio.wait_for(entered.wait(), 2)
        mutated = asyncio.Event()

        async def mark() -> None:
            mutated.set()

        await asyncio.wait_for(sub.res.coordinator.run(["bob"], mark, name="test:unrelated"), 2)
        blocked = asyncio.create_task(sub.res.coordinator.run(["alice"], mark, name="test:delta"))
        await asyncio.sleep(0)
        assert not blocked.done()
        release.set()
        await poll
        await blocked
        assert mutated.is_set()
    finally:
        release.set()
        await watch.close()
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_restart_counts_offline_traffic_once(tmp_path: Path) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    restarted = BWatch(sub.res.cfg, db, sub, scheduled=True)
    total = 100

    async def traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
        require_complete: bool = False,
        known: Mapping[str, str] | None = None,
    ) -> dict[str, dict[str, BandwidthInfo]]:
        return {"panel": {"alice": BandwidthInfo(total, 0, total)}}

    async def no_snapshots(now: float) -> None:
        return None

    sub.bandwidth_svc.panel_traffic = traffic  # type: ignore[method-assign]
    restarted._run_due_daily_snapshots = no_snapshots  # type: ignore[method-assign]
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1,
            wl_limit_gb=1,
        )
        await watch.is_first()
        await watch.start()
        await watch.bandwidth_check()
        total = 130
        await watch.bandwidth_check()
        await watch.close()
        total = 180
        await restarted.start()
        await restarted.bandwidth_check()
        state = await db.get_user("alice")
        assert state is not None
        assert (state["bw_used"], state["wl_used"]) == (80, 80)
    finally:
        await watch.close()
        await restarted.close()
        await close_subscription(sub)
        await db.close()


@pytest.mark.asyncio
async def test_failed_startup_poll_retains_baseline_until_recovery(tmp_path: Path) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    restarted = BWatch(sub.res.cfg, db, sub, scheduled=True)
    total = 100
    offline = False

    async def traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
        require_complete: bool = False,
        known: Mapping[str, str] | None = None,
    ) -> dict[str, dict[str, BandwidthInfo]]:
        if offline:
            raise PanelUnavailableError("panel offline")
        return {"panel": {"alice": BandwidthInfo(total, 0, total)}}

    async def no_snapshots(now: float) -> None:
        return None

    sub.bandwidth_svc.panel_traffic = traffic  # type: ignore[method-assign]
    restarted._run_due_daily_snapshots = no_snapshots  # type: ignore[method-assign]
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1,
        )
        await watch.start()
        await watch.bandwidth_check()
        await watch.close()
        total = 150
        offline = True
        await restarted.start()
        assert restarted.mem["panel", "alice", USER_UUID].total == 100
        offline = False
        await restarted.bandwidth_check()
        state = await db.get_user("alice")
        assert state is not None
        assert state["bw_used"] == 50
    finally:
        await watch.close()
        await restarted.close()
        await close_subscription(sub)
        await db.close()
