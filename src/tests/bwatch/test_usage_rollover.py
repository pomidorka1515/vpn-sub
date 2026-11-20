from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from bwatch.watch import BWatch
from custom_types import BandwidthInfo
from db import Database
from tests.support.panels import FakePanel, make_panel_client
from tests.support.subscriptions import close_subscription, make_subscription
from tests.support.users import USER_UUID

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


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
