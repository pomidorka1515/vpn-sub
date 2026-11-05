from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from async_core_helpers import close_async_subscription, make_async_subscription
from helpers import USER_UUID, FakePanel, make_panel_client

from bwatch.watch import BWatch
from custom_types import BandwidthInfo
from db import Database
from errors import PanelUnavailableError

if TYPE_CHECKING:
    from pathlib import Path


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
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    total = 100
    missing = False

    async def traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
        require_complete: bool = False,
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
        await close_async_subscription(sub)
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
    sub = await make_async_subscription(db, tmp_path, panels=panels[:2], whitelist=panels[2])
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    recovery = AsyncMock()
    watch._quota = recovery  # type: ignore[method-assign]

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
        await watch.bandwidth_check()
        readings(140)
        for panel in panels:
            if panel.name in failed:
                panel._get_error = RuntimeError("offline")
        recovery.reset_mock()
        await watch.bandwidth_check()
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
        await watch.bandwidth_check()
        for panel in panels:
            panel._get_error = None
        readings(160)
        await watch.bandwidth_check()
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
        await watch.bandwidth_check()
        recovery.assert_awaited_once_with("alice", recover=True)
        state = await db.get_user("alice")
        assert state is not None
        assert (state["bw_used"], state["wl_used"]) == (0, 0)
        assert await db.get_metadata("last_reset_month") == "2026-10"
        for panel in panels:
            panel._get_error = None
        await watch.bandwidth_check()  # Returning panels need new-month baselines.
        readings(925)
        await watch.bandwidth_check()
        state = await db.get_user("alice")
        assert state is not None
        assert (state["bw_used"], state["wl_used"]) == (50, 25)
    finally:
        await watch.close()
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
async def test_accounting_persists_before_advancing_baselines(tmp_path: Path) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
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
        await watch.start()
        watch.mem["panel", "alice", USER_UUID] = BandwidthInfo(10, 20, 30)

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
        ) -> dict[str, dict[str, BandwidthInfo]]:
            return {"panel": await traffic(whitelist, work=work)}

        sub.bandwidth_svc.panel_traffic = panel_traffic  # type: ignore[method-assign]
        increment = db.increment_usages

        async def failed_commit(
            updates: object,
            *,
            baselines: str | None = None,
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
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
async def test_counter_reset_confirmation_and_restart(tmp_path: Path) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    restarted = BWatch(sub.res.cfg, db, sub, scheduled=False)
    totals: dict[str, int | None] = {"main": 100, "whitelist": 200}

    async def traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
        require_complete: bool = False,
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
        await close_async_subscription(sub)
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
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    totals: tuple[int | None, int] = readings[0]

    async def traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
        require_complete: bool = False,
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
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
async def test_usage_baselines_do_not_cross_account_identities(tmp_path: Path) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    total = 100

    async def traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
        require_complete: bool = False,
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
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
async def test_restart_counts_offline_traffic_once(tmp_path: Path) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    restarted = BWatch(sub.res.cfg, db, sub, scheduled=True)
    total = 100

    async def traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
        require_complete: bool = False,
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
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
async def test_failed_startup_poll_retains_baseline_until_recovery(tmp_path: Path) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    restarted = BWatch(sub.res.cfg, db, sub, scheduled=True)
    total = 100
    offline = False

    async def traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
        require_complete: bool = False,
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
        await close_async_subscription(sub)
        await db.close()
