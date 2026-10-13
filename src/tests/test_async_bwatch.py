from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from async_core_helpers import close_async_subscription, make_async_subscription
from helpers import USER_UUID

from bwatch.watch import BWatch
from custom_types import BandwidthInfo
from db import Database
from notify import classify
from notify.notifier import Notifier

if TYPE_CHECKING:
    from pathlib import Path


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
            whitelist: bool = False, *, work: str = "foreground",
        ) -> dict[str, BandwidthInfo]:
            return {"alice": BandwidthInfo(20, 40, 60)}

        sub.bandwidth_svc.all_traffic = traffic  # type: ignore[method-assign]
        increment = db.increment_usages

        async def failed_commit(updates: object) -> None:
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
            texts={"en": {notice.kind.text_key: "warning"}}, send=send, language=language,
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
