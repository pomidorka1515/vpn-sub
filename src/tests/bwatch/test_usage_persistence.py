from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from bwatch.watch import BWatch
from custom_types import BandwidthInfo
from db import Database
from errors import PanelUnavailableError
from tests.support.subscriptions import close_subscription, make_subscription
from tests.support.users import USER_UUID

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from db.state import UsageBaselineKey, UsageBaselines


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
            raise PanelUnavailableError("panel offline") from None
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
