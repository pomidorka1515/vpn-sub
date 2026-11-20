from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from bwatch.watch import BWatch
from custom_types import BandwidthInfo
from db import Database
from tests.support.subscriptions import close_subscription, make_subscription
from tests.support.users import USER_UUID

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from db.state import UsageBaselineKey, UsageBaselines


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
            raise RuntimeError("disk unavailable") from None

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
        raise failure from None

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
