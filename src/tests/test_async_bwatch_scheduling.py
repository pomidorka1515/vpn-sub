from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from async_core_helpers import close_async_subscription, make_async_subscription

from bwatch.watch import BWatch
from db import Database

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_daily_schedule_realigns_to_midnight_after_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    now = 86400.0 + 12 * 3600
    monotonic = 100.0
    monkeypatch.setattr("bwatch.watch.time.time", lambda: now)
    # Keep the event loop's real monotonic clock untouched.
    try:
        watch.record_daily_snapshot = AsyncMock(side_effect=RuntimeError("offline"))  # type: ignore[method-assign]
        watch.record_snap_snapshot = AsyncMock()  # type: ignore[method-assign]
        before = time.monotonic()
        await watch._run_due_daily_snapshots(monotonic)
        after = time.monotonic()
        assert before + 3600 <= watch._snapshot_due_at["bandwidth"] <= after + 3600
        assert before + 12 * 3600 <= watch._snapshot_due_at["state"] <= after + 12 * 3600

        # A successful late retry targets the next midnight, not retry time + 24h.
        now += 3600
        watch.record_daily_snapshot = AsyncMock()  # type: ignore[method-assign]
        due = watch._snapshot_due_at["bandwidth"]
        before = time.monotonic()
        await watch._run_due_daily_snapshots(due)
        after = time.monotonic()
        assert before + 11 * 3600 <= watch._snapshot_due_at["bandwidth"] <= after + 11 * 3600
        assert not watch._snapshot_failures
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
