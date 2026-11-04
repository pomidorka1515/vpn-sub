from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from async_core_helpers import close_async_subscription, make_async_subscription
from helpers import USER_UUID

from bwatch.watch import BWatch
from custom_types import BandwidthInfo
from db import Database
from errors import PanelUnavailableError
from notify import Kind, classify
from notify.notifier import Notifier

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("whitelist", [False, True])
@pytest.mark.parametrize("exhausted", [False, True])
async def test_poll_preserves_explicit_disable(
    tmp_path: Path, whitelist: bool, exhausted: bool,
) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    notices = AsyncMock()
    watch._deliver = notices  # type: ignore[method-assign]
    try:
        await db.create_user(
            username="alice", uuid=USER_UUID, token="a" * 40,
            fingerprint="chrome", displayname="Alice", bw_limit_gb=1, wl_limit_gb=1,
        )
        if exhausted:
            await db.increment_usage("alice", regular=10**9, whitelist=10**9)
            await watch.check()
        await sub.business_svc.update_user(
            "alice", enable=None if whitelist else False,
            wl_enable=False if whitelist else None,
        )
        # Recovery after a reset, including a fresh watcher, must respect intent.
        await db.update_user("alice", bw_used=0, wl_bw_used=0)
        await watch.close()
        watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
        watch._deliver = notices  # type: ignore[method-assign]
        notices.reset_mock()
        await watch.bandwidth_check()
        state = await db.get_user("alice")
        assert state is not None
        assert not state["enabled_wl" if whitelist else "enabled"]
        assert state["enabled" if whitelist else "enabled_wl"]
        blocked_notice = Kind.WHITELIST_RESTORED if whitelist else Kind.RESTORED
        for call in notices.await_args_list:
            assert all(notice.kind is not blocked_notice for notice in call.args[1])
        # The shared quota helper must obey the same rule as polling.
        await sub.business_svc.apply_quota("alice")
        state = await db.get_user("alice")
        assert state is not None
        assert not state["enabled_wl" if whitelist else "enabled"]
        await sub.business_svc.update_user(
            "alice", enable=None if whitelist else True,
            wl_enable=True if whitelist else None,
        )
        await watch.bandwidth_check()
        state = await db.get_user("alice")
        assert state is not None
        assert state["enabled"]
        assert state["enabled_wl"]
    finally:
        await watch.close()
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
async def test_quota_disable_recovers_after_reset(tmp_path: Path) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    try:
        await db.create_user(
            username="alice", uuid=USER_UUID, token="a" * 40,
            fingerprint="chrome", displayname="Alice", bw_limit_gb=1, wl_limit_gb=1,
        )
        await db.increment_usage("alice", regular=10**9, whitelist=10**9)
        await watch.check()
        state = await db.get_user("alice")
        assert state is not None
        assert not state["enabled"]
        assert not state["enabled_wl"]
        assert not state["admin_disabled"]
        assert not state["admin_disabled_wl"]
        await db.update_user("alice", bw_used=0, wl_bw_used=0)
        await watch.bandwidth_check()
        state = await db.get_user("alice")
        assert state is not None
        assert state["enabled"]
        assert state["enabled_wl"]
    finally:
        await watch.close()
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
async def test_monthly_reset_catches_up_once_after_outage(
    database: Database, watch: BWatch, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> Clock:
            return cls(2026, 10, 3, tzinfo=UTC)

    monkeypatch.setattr("bwatch.watch.datetime", Clock)
    await database.create_user(
        username="alice", uuid=USER_UUID, token="a" * 40, fingerprint="chrome",
        displayname="Alice", bw_limit_gb=10, wl_limit_gb=10,
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
        watch.mem["alice"] = BandwidthInfo(10, 20, 30)

        async def traffic(
            whitelist: bool = False,
            *,
            work: str = "foreground",
        ) -> dict[str, BandwidthInfo]:
            return {"alice": BandwidthInfo(20, 40, 60)}

        sub.bandwidth_svc.all_traffic = traffic  # type: ignore[method-assign]
        increment = db.increment_usages

        async def failed_commit(updates: object, *, baselines: str | None = None) -> None:
            raise RuntimeError("disk unavailable")

        db.increment_usages = failed_commit  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="disk unavailable"):
            await watch.bandwidth_check()
        assert watch.mem["alice"].total == 30
        db.increment_usages = increment  # type: ignore[method-assign]
        await watch.bandwidth_check()
        await watch.bandwidth_check()
        assert (await db.get_user("alice"))["bw_used"] == 30  # type: ignore[index]
        watch.snap_mem["alice"] = BandwidthInfo(10, 20, 30)
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
        whitelist: bool = False, *, work: str = "foreground",
    ) -> dict[str, BandwidthInfo]:
        total = totals["whitelist" if whitelist else "main"]
        return {} if total is None else {"alice": BandwidthInfo(total, 0, total)}

    sub.bandwidth_svc.all_traffic = traffic  # type: ignore[method-assign]
    try:
        await db.create_user(
            username="alice", uuid=USER_UUID, token="a" * 40,
            fingerprint="chrome", displayname="Alice", bw_limit_gb=1, wl_limit_gb=1,
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
        assert (state["bw_used"], state["wl_used"]) == (17, 19)
        totals.update(main=None, whitelist=None)
        await restarted.bandwidth_check()
        assert restarted.mem["alice"].total == 12
        assert restarted.wl_mem["alice"].total == 16
    finally:
        await watch.close()
        await restarted.close()
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
async def test_job_serialization_and_failure_retry(tmp_path: Path) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def job() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()

    try:
        first = asyncio.create_task(watch._job("test", job))
        await entered.wait()
        second = asyncio.create_task(watch._job("test", job))
        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        await asyncio.gather(first, second)
        assert calls == 2

        async def fail() -> None:
            raise RuntimeError("offline")

        watch.record_daily_snapshot = fail  # type: ignore[method-assign]
        watch.record_snap_snapshot = job  # type: ignore[method-assign]
        await watch._run_due_daily_snapshots(0)
        assert watch._snapshot_failures == {"bandwidth": 1}
        assert watch._snapshot_due_at["bandwidth"] > 0
    finally:
        await watch.close()
        await watch.close()
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
async def test_notice_failure_and_concurrent_delivery(tmp_path: Path) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1,
            wl_limit_gb=0,
        )
        await db.increment_usages({"alice": (10**9, 0)})
        state = await db.get_user("alice")
        assert state is not None
        notice = classify(state, 0)[0]
        calls = 0

        async def send(_id: int, _text: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("offline")
            await asyncio.sleep(0)

        async def language(_id: int) -> str:
            return "en"

        notifier = Notifier(
            texts={"en": {notice.kind.text_key: "warning"}},
            send=send,
            language=language,
        )
        seen: set[str] = set()
        assert not await notifier.deliver("alice", 1, notice, seen)
        assert not seen
        assert (
            sum(
                await asyncio.gather(
                    *(notifier.deliver("alice", 1, notice, seen) for _ in range(2)),
                ),
            )
            == 1
        )
        assert calls == 2
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("expiry_offset", [-1, 0, 1, None])
async def test_recovery_only_restores_unexpired_accounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expiry_offset: int | None,
) -> None:
    now = int(time.time())
    monkeypatch.setattr("bwatch.watch.time.time", lambda: now)
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
            expires_at=now + expiry_offset if expiry_offset is not None else 0,
            bw_limit_gb=10,
            wl_limit_gb=0,
        )
        await watch.check()
        # Also cover recovery from a traffic disable on a valid subscription.
        await db.update_user("alice", status=False, status_time=False)
        state = await db.get_user("alice")
        assert state is not None
        assert not state["enabled"]
        assert not state["enabled_time"]
        for _ in range(2):
            await watch.bandwidth_check()
            await watch.check()
            state = await db.get_user("alice")
            assert state is not None
            expected = expiry_offset is None or expiry_offset > 0
            assert bool(state["enabled"]) is expected
            assert bool(state["enabled_time"]) is expected
    finally:
        await watch.close()
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
async def test_quota_rereads_limits_after_waiting_for_admin(tmp_path: Path) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1,
            wl_limit_gb=0,
        )
        await db.increment_usages({"alice": (10**9, 0)})

        async def admin() -> None:
            entered.set()
            await release.wait()
            await sub.business_svc.update_params("alice", limit=2)

        editing = asyncio.create_task(sub.res.coordinator.run(["alice"], admin, name="test:admin"))
        await entered.wait()
        checking = asyncio.create_task(watch.check())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(editing, checking)
        state = await db.get_user("alice")
        assert state is not None
        assert state["bw_limit_gb"] == 2
        assert state["enabled"]
    finally:
        await watch.close()
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
async def test_expiry_recovery_keeps_exhausted_main_disabled(tmp_path: Path) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    try:
        await db.create_user(
            username="alice", uuid=USER_UUID, token="a" * 40,
            fingerprint="chrome", displayname="Alice", bw_limit_gb=1,
        )
        await db.increment_usage("alice", regular=10**9)
        await db.update_user("alice", status=False, status_time=False)
        await watch.bandwidth_check()
        state = await db.get_user("alice")
        assert state is not None
        assert not state["enabled"]
        assert state["enabled_time"]
    finally:
        await watch.close()
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_unlimited_quotas_restore_access(tmp_path: Path, expired: bool) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    try:
        await db.create_user(
            username="alice", uuid=USER_UUID, token="a" * 40,
            fingerprint="chrome", displayname="Alice", bw_limit_gb=1, wl_limit_gb=1,
            expires_at=int(time.time()) - 1 if expired else 0,
        )
        await db.increment_usage("alice", regular=10**9, whitelist=10**9)
        await db.update_user("alice", status=False, status_wl=False)
        await sub.business_svc.update_params("alice", limit=0, wl_limit=0)
        await watch.bandwidth_check()
        state = await db.get_user("alice")
        assert state is not None
        assert bool(state["enabled"]) is not expired
        assert state["enabled_wl"]
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
        whitelist: bool = False, *, work: str = "foreground",
    ) -> dict[str, BandwidthInfo]:
        return {"alice": BandwidthInfo(total, 0, total)}

    async def no_snapshots(now: float) -> None:
        return None

    sub.bandwidth_svc.all_traffic = traffic  # type: ignore[method-assign]
    restarted._run_due_daily_snapshots = no_snapshots  # type: ignore[method-assign]
    try:
        await db.create_user(
            username="alice", uuid=USER_UUID, token="a" * 40,
            fingerprint="chrome", displayname="Alice", bw_limit_gb=1, wl_limit_gb=1,
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
        whitelist: bool = False, *, work: str = "foreground",
    ) -> dict[str, BandwidthInfo]:
        if offline:
            raise PanelUnavailableError("panel offline")
        return {"alice": BandwidthInfo(total, 0, total)}

    async def no_snapshots(now: float) -> None:
        return None

    sub.bandwidth_svc.all_traffic = traffic  # type: ignore[method-assign]
    restarted._run_due_daily_snapshots = no_snapshots  # type: ignore[method-assign]
    try:
        await db.create_user(
            username="alice", uuid=USER_UUID, token="a" * 40,
            fingerprint="chrome", displayname="Alice", bw_limit_gb=1,
        )
        await watch.start()
        await watch.bandwidth_check()
        await watch.close()
        total = 150
        offline = True
        await restarted.start()
        assert restarted.mem["alice"].total == 100
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
