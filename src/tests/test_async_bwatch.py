from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from async_core_helpers import close_async_subscription, make_async_subscription
from helpers import USER_UUID, FakePanel, make_inbound

from bwatch.watch import BWatch
from custom_types import BandwidthInfo
from db import Database
from errors import PanelUnavailableError
from notify import Kind, classify
from notify.notifier import Notifier

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
async def test_state_samples_preserve_collection_times_across_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path, panels=[])
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    now = int(time.time()) // 86400 * 86400 + 3600
    first = now
    monkeypatch.setattr("bwatch.watch.time.time", lambda: now)
    try:
        await watch.record_snap_snapshot()
        await watch.close()
        now += 3600
        watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
        await watch.record_snap_snapshot()
        rows = await db.get_state_snapshots(0)
        assert [row["ts"] for row in rows] == [now, first]
    finally:
        await watch.close()
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("whitelist", [False, True])
@pytest.mark.parametrize("exhausted", [False, True])
async def test_poll_preserves_explicit_disable(
    tmp_path: Path,
    whitelist: bool,
    exhausted: bool,
) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    notices = AsyncMock()
    watch._deliver = notices  # type: ignore[method-assign]
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
        if exhausted:
            await db.increment_usage("alice", regular=10**9, whitelist=10**9)
            await watch.check()
        await sub.business_svc.update_user(
            "alice",
            enable=None if whitelist else False,
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
            "alice",
            enable=None if whitelist else True,
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
@pytest.mark.parametrize("reason", ["expiry", "traffic", "whitelist", "combined"])
@pytest.mark.parametrize("restart", [False, True])
async def test_quota_check_retries_failed_disable_without_terminal_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    restart: bool,
) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    main = FakePanel(name="main", inbounds=[make_inbound(1)])
    whitelist = FakePanel(name="whitelist", mode="whitelist", inbounds=[make_inbound(2)])
    sub = await make_async_subscription(db, tmp_path, panels=[main], whitelist=whitelist)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    expired = reason in {"expiry", "combined"}
    exhausted = reason in {"traffic", "combined"}
    wl_exhausted = reason in {"whitelist", "combined"}
    failed_panel = whitelist if reason == "whitelist" else main
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            expires_at=int(time.time()) - 1 if expired else 0,
            bw_limit_gb=1 if exhausted else 0,
            wl_limit_gb=1 if wl_exhausted else 0,
        )
        await sub.business_svc.add_users("alice")
        await db.increment_usage(
            "alice",
            regular=10**9 if exhausted else 0,
            whitelist=10**9 if wl_exhausted else 0,
        )
        monkeypatch.setattr(failed_panel, "_post_payload", {"success": False, "msg": "offline"})
        await watch.check()
        state = await db.get_user("alice")
        assert state is not None
        assert bool(state["enabled"]) is not (expired or exhausted)
        assert bool(state["enabled_time"]) is not expired
        assert bool(state["enabled_wl"]) is not wl_exhausted
        assert not classify(state, int(time.time()))
        assert await db.get_metadata("user_sync_pending:alice") is not None
        assert failed_panel.clients[0].enable

        if restart:
            await watch.close()
            watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
        reconcile = AsyncMock(wraps=sub.business_svc.reconcile_user)
        monkeypatch.setattr(sub.business_svc, "reconcile_user", reconcile)
        # A second failed check must retain the marker for another retry.
        await watch.check()
        assert reconcile.await_count == 1
        assert await db.get_metadata("user_sync_pending:alice") is not None
        assert failed_panel.clients[0].enable

        monkeypatch.setattr(failed_panel, "_post_payload", None)
        await watch.check()
        assert reconcile.await_count == 2
        assert await db.get_metadata("user_sync_pending:alice") is None
        assert main.clients[0].enable is not (expired or exhausted)
        assert whitelist.clients[0].enable is not wl_exhausted
        await watch.check()
        assert reconcile.await_count == 2
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
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1,
            wl_limit_gb=1,
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
    database: Database,
    watch: BWatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> Clock:
            return cls(2026, 10, 3, tzinfo=UTC)

    monkeypatch.setattr("bwatch.watch.datetime", Clock)
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

    monkeypatch.setattr("bwatch.watch.datetime", Clock)
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
    ) -> dict[str, dict[str, BandwidthInfo]]:
        return {} if missing else {"panel": {"alice": BandwidthInfo(total, 0, total)}}

    async def no_snapshots(now: float) -> None:
        return None

    sub.bandwidth_svc.panel_traffic = traffic  # type: ignore[method-assign]
    try:
        await db.create_user(
            username="alice", uuid=USER_UUID, token="a" * 40,
            fingerprint="chrome", displayname="Alice", bw_limit_gb=1, wl_limit_gb=1,
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
        whitelist: bool = False,
        *,
        work: str = "foreground",
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
@pytest.mark.parametrize("exhausted", [False, True])
@pytest.mark.parametrize("whitelist_exhausted", [False, True])
async def test_quota_applies_combined_terminal_state_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expiry_offset: int | None,
    exhausted: bool,
    whitelist_exhausted: bool,
) -> None:
    now = int(time.time())
    monkeypatch.setattr("bwatch.watch.time.time", lambda: now)
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    update = AsyncMock(wraps=sub.business_svc.update_user)
    monkeypatch.setattr(sub.business_svc, "update_user", update)
    notices = AsyncMock()
    watch._deliver = notices  # type: ignore[method-assign]
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1,
            wl_limit_gb=1,
            expires_at=now + expiry_offset if expiry_offset is not None else 0,
        )
        await db.increment_usage(
            "alice",
            regular=10**9 if exhausted else 0,
            whitelist=10**9 if whitelist_exhausted else 0,
        )
        await watch.check()
        expired = expiry_offset is not None and expiry_offset <= 0
        state = await db.get_user("alice")
        assert state is not None
        assert bool(state["enabled"]) is not (expired or exhausted)
        assert bool(state["enabled_time"]) is not expired
        assert bool(state["enabled_wl"]) is not whitelist_exhausted
        expected_terminal: set[Kind] = set()
        if expired:
            expected_terminal.add(Kind.EXPIRED)
        if exhausted:
            expected_terminal.add(Kind.TRAFFIC_EXHAUSTED)
        if whitelist_exhausted:
            expected_terminal.add(Kind.WHITELIST_EXHAUSTED)
        assert update.await_count == int(bool(expected_terminal))
        delivered = {notice.kind for notice in notices.await_args_list[0].args[1]}
        assert (
            delivered
            & {
                Kind.EXPIRED,
                Kind.TRAFFIC_EXHAUSTED,
                Kind.WHITELIST_EXHAUSTED,
            }
            == expected_terminal
        )
        await watch.bandwidth_check()
        await watch.check()
        state = await db.get_user("alice")
        assert state is not None
        assert bool(state["enabled"]) is not (expired or exhausted)
        assert bool(state["enabled_time"]) is not expired
        assert update.await_count == int(bool(expected_terminal))
    finally:
        await watch.close()
        await close_async_subscription(sub)
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
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1,
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
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1,
            wl_limit_gb=1,
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
        whitelist: bool = False,
        *,
        work: str = "foreground",
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
async def test_daily_history_survives_repeated_scheduled_restarts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=True)
    total = 100
    now = int(time.time()) // 86400 * 86400 + 3600
    first = now
    monkeypatch.setattr("bwatch.watch.time.time", lambda: now)

    async def traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
    ) -> dict[str, BandwidthInfo]:
        return {"alice": BandwidthInfo(total, total * 2, total * 3)}

    async def panel_traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
    ) -> dict[str, dict[str, BandwidthInfo]]:
        return {"panel": await traffic(whitelist, work=work)}

    sub.bandwidth_svc.all_traffic = traffic  # type: ignore[method-assign]
    sub.bandwidth_svc.panel_traffic = panel_traffic  # type: ignore[method-assign]
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
        watch.record_snap_snapshot = AsyncMock()  # type: ignore[method-assign]
        await watch.start()
        for reading in (150, 180, 180):
            now += 3600
            total = reading
            # Usage polling advances independently of the last daily snapshot.
            await watch.bandwidth_check()
            await watch.close()
            watch = BWatch(sub.res.cfg, db, sub, scheduled=True)
            watch.record_snap_snapshot = AsyncMock()  # type: ignore[method-assign]
            await watch.start()
        rows = await db.get_bandwidth_snapshots("alice", 0)
        assert [row["ts"] for row in rows] == [first + i * 3600 for i in (3, 2, 1, 0)]
        assert sum(row["up"] for row in rows) == 80
        assert sum(row["down"] for row in rows) == 160
        assert sum(row["wl_up"] for row in rows) == 80
        assert sum(row["wl_down"] for row in rows) == 160
    finally:
        await watch.close()
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("readings", [(100, 150), (100, 10, 15)])
async def test_daily_snapshot_retry_reloads_durable_baselines(
    tmp_path: Path,
    committed: bool,
    readings: tuple[int, ...],
) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    total = 100

    async def traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
    ) -> dict[str, BandwidthInfo]:
        return {"alice": BandwidthInfo(total, total * 2, total * 3)}

    sub.bandwidth_svc.all_traffic = traffic  # type: ignore[method-assign]
    save = db.add_bandwidth_snapshots

    async def interrupted(
        rows: list[tuple[str, int, int, int, int, int]],
        *,
        baselines: str | None = None,
    ) -> None:
        if committed:
            await save(rows, baselines=baselines)
        raise asyncio.CancelledError

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
        for reading in readings[:-1]:
            total = reading
            await watch.record_daily_snapshot()
        total = readings[-1]
        db.add_bandwidth_snapshots = interrupted  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await watch.record_daily_snapshot()
        assert watch.snap_mem["alice"].upload == readings[0]
        db.add_bandwidth_snapshots = save  # type: ignore[method-assign]
        await watch.record_daily_snapshot()
        rows = await db.get_bandwidth_snapshots("alice", 0)
        expected = 50 if len(readings) == 2 else 15
        assert sum(row["up"] for row in rows) == expected
        assert sum(row["wl_up"] for row in rows) == expected
        assert sum(row["down"] for row in rows) == expected * 2
        assert sum(row["wl_down"] for row in rows) == expected * 2
    finally:
        await watch.close()
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("readings", "expected"),
    [
        ([100, 10, 15, 20], 20),
        ([100, 10, 110], 10),
        ([100, 10, None, 15, 20], 20),
        ([100, 10, 5, 8], 8),
        ([100, 0, 0, 5], 5),
    ],
)
async def test_daily_snapshot_reset_confirmation_survives_restart(
    tmp_path: Path,
    readings: list[int | None],
    expected: int,
) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    total: int | None = None

    async def traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
    ) -> dict[str, BandwidthInfo]:
        return {} if total is None else {"alice": BandwidthInfo(total, total * 2, total * 3)}

    sub.bandwidth_svc.all_traffic = traffic  # type: ignore[method-assign]
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
        for reading in readings:
            total = reading
            await watch.start()
            await watch.record_daily_snapshot()
            await watch.close()
            watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
        rows = await db.get_bandwidth_snapshots("alice", 0)
        assert sum(row["up"] for row in rows) == expected
        assert sum(row["down"] for row in rows) == expected * 2
        assert sum(row["wl_up"] for row in rows) == expected
        assert sum(row["wl_down"] for row in rows) == expected * 2
    finally:
        await watch.close()
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
