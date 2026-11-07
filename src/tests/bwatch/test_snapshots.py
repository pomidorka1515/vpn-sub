from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
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
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertainty", ["legacy", "missing", "reset"])
async def test_snapshot_does_not_guess_incompatible_or_unknown_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    uncertainty: str,
) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    panel = FakePanel(name="panel")
    sub = await make_subscription(db, tmp_path, panels=[panel])
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    now = int(time.time())
    monkeypatch.setattr("bwatch.snapshots.time", SimpleNamespace(time=lambda: now))

    async def sample(value: int | None) -> None:
        nonlocal now
        now += 60
        panel.clients = [] if value is None else [make_panel_client("alice", [1], up=value)]
        await watch.record_daily_snapshot()

    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1,
        )
        await sample(100)
        await sample(150)
        assert (await db.get_bandwidth_snapshots("alice", 0))[0].get("start_ts") == now - 60
        if uncertainty == "legacy":
            saved = json.loads(await db.get_metadata("watcher_snapshot_baselines") or "{}")
            saved.pop("sample_times")
            await db.set_metadata("watcher_snapshot_baselines", json.dumps(saved))
        await sample(None if uncertainty == "missing" else 10 if uncertainty == "reset" else 200)
        assert (await db.get_bandwidth_snapshots("alice", 0))[0].get("start_ts") is None
        await sample(20 if uncertainty == "reset" else 250)
        if uncertainty == "reset":
            assert (await db.get_bandwidth_snapshots("alice", 0))[0].get("start_ts") is None
        await sample(30 if uncertainty == "reset" else 300)
        assert (await db.get_bandwidth_snapshots("alice", 0))[0].get("start_ts") == now - 60
    finally:
        await watch.close()
        await close_subscription(sub)
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outage", ["one", "main", "whitelist", "all"])
@pytest.mark.parametrize("restart", [False, True])
async def test_daily_snapshot_outage_retries_complete_sample(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outage: str,
    restart: bool,
) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    panels = [FakePanel(name=name) for name in ("one", "two", "whitelist")]
    sub = await make_subscription(db, tmp_path, panels=panels[:2], whitelist_panel=panels[2])
    alert = AsyncMock()
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False, alert=alert)
    now = int(time.time()) // 86400 * 86400 + 3600
    first = now
    monkeypatch.setattr("bwatch.snapshots.time", SimpleNamespace(time=lambda: now))
    failed = {
        "one": {"one"},
        "main": {"one", "two"},
        "whitelist": {"whitelist"},
        "all": {"one", "two", "whitelist"},
    }[outage]

    def readings(total: int) -> None:
        for panel in panels:
            panel.clients = [make_panel_client("alice", [1], up=total, down=total * 2)]

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
        await watch.start()
        await watch.record_daily_snapshot()
        saved = await db.get_metadata("watcher_snapshot_baselines")
        baseline_main, baseline_wl = dict(watch.snap_mem), dict(watch.snap_wl_mem)
        now += 86400
        readings(140)
        for panel in panels:
            if panel.name in failed:
                panel._get_error = RuntimeError("offline")
        with pytest.raises(PanelUnavailableError, match="1/1 eligible"):
            await watch.record_daily_snapshot()
        failure = {"ts": now, "failed": 1, "eligible": 1}
        assert watch.get_daily_snapshot_failure() == failure
        assert await db.get_metadata("daily_bw_snapshot_failures") == f"{now}:1:1"

        watch.record_snap_snapshot = AsyncMock()  # type: ignore[method-assign]
        before = time.monotonic()
        await watch._run_due_daily_snapshots(0)
        after = time.monotonic()
        assert before + 3600 <= watch._snapshot_due_at["bandwidth"] <= after + 3600
        assert watch._snapshot_failures == {"bandwidth": 1}
        alert.assert_awaited_once()
        assert watch.snap_mem == baseline_main
        assert watch.snap_wl_mem == baseline_wl
        assert await db.get_metadata("watcher_snapshot_baselines") == saved
        rows = await db.get_bandwidth_snapshots("alice", 0)
        assert [row["ts"] for row in rows] == [first]

        if restart:
            await watch.close()
            watch = BWatch(sub.res.cfg, db, sub, scheduled=False, alert=alert)
            await watch.start()
            assert watch.get_daily_snapshot_failure() == failure
            assert watch.snap_mem == baseline_main
            assert watch.snap_wl_mem == baseline_wl
        for panel in panels:
            panel._get_error = None
        now += 3600
        readings(160)
        watch.record_snap_snapshot = AsyncMock()  # type: ignore[method-assign]
        watch._snapshot_due_at["bandwidth"] = 0
        await watch._run_due_daily_snapshots(0)
        assert not watch._snapshot_failures
        assert watch.get_daily_snapshot_failure() is None
        assert await db.get_metadata("daily_bw_snapshot_failures") is None
        rows = await db.get_bandwidth_snapshots("alice", 0)
        assert [row["ts"] for row in rows] == [now, first]
        assert rows[0].get("start_ts") == first
        history = await sub.bandwidth_svc.get_bw_history("alice")
        assert history[0].start_ts == first
        assert history[-1].start_ts is None
        assert (rows[0]["up"], rows[0]["down"], rows[0]["wl_up"], rows[0]["wl_down"]) == (
            120,
            240,
            60,
            120,
        )

        # Reopening after recovery and repeating the read cannot count catch-up twice.
        await watch.close()
        watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
        await watch.start()
        assert watch.get_daily_snapshot_failure() is None
        now += 60
        await watch.record_daily_snapshot()
        rows = await db.get_bandwidth_snapshots("alice", 0)
        assert sum(row["up"] for row in rows) == 120
        assert sum(row["down"] for row in rows) == 240
        assert sum(row["wl_up"] for row in rows) == 60
        assert sum(row["wl_down"] for row in rows) == 120
        assert rows[0].get("start_ts") == now - 60
    finally:
        await watch.close()
        await close_subscription(sub)
        await db.close()


@pytest.mark.asyncio
async def test_state_samples_preserve_collection_times_across_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_subscription(db, tmp_path, panels=[])
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    now = int(time.time()) // 86400 * 86400 + 3600
    first = now
    monkeypatch.setattr("bwatch.snapshots.time", SimpleNamespace(time=lambda: now))
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
        await close_subscription(sub)
        await db.close()


@pytest.mark.asyncio
async def test_daily_history_survives_repeated_scheduled_restarts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=True)
    total = 100
    now = int(time.time()) // 86400 * 86400 + 3600
    first = now
    monkeypatch.setattr("bwatch.snapshots.time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(
        "bwatch.watch.time", SimpleNamespace(time=lambda: now, monotonic=time.monotonic)
    )

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
        require_complete: bool = False,
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
        await close_subscription(sub)
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
    sub = await make_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    total = 100

    async def traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
        require_complete: bool = False,
    ) -> dict[str, dict[str, BandwidthInfo]]:
        return {"panel": {"alice": BandwidthInfo(total, total * 2, total * 3)}}

    sub.bandwidth_svc.panel_traffic = traffic  # type: ignore[method-assign]
    save = db.add_bandwidth_snapshots

    async def interrupted(
        rows: list[tuple[str, int, int, int, int, int]],
        *,
        baselines: str | None = None,
        starts: dict[str, int | None] | None = None,
    ) -> None:
        if committed:
            await save(rows, baselines=baselines, starts=starts)
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
        assert watch.snap_mem["panel", "alice", USER_UUID].upload == readings[0]
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
        await close_subscription(sub)
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
    sub = await make_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    total: int | None = None

    async def traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
        require_complete: bool = False,
    ) -> dict[str, dict[str, BandwidthInfo]]:
        return (
            {}
            if total is None
            else {"panel": {"alice": BandwidthInfo(total, total * 2, total * 3)}}
        )

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
        await close_subscription(sub)
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outage", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
async def test_daily_history_keeps_panel_reset_domains_independent(
    tmp_path: Path,
    outage: bool,
    legacy: bool,
) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    readings: dict[str, int] = {"a": 1000, "b": 1000}

    async def traffic(
        whitelist: bool = False,
        *,
        work: str = "foreground",
        require_complete: bool = False,
    ) -> dict[str, dict[str, BandwidthInfo]]:
        if require_complete and "a" not in readings:
            raise PanelUnavailableError("panel a unavailable")
        return {
            panel: {"alice": BandwidthInfo(total, total * 2, total * 3)}
            for panel, total in readings.items()
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
        if legacy:
            await db.set_metadata(
                "watcher_snapshot_baselines",
                json.dumps({"main": [["alice", USER_UUID, 2000, 4000, 6000]]}),
            )
        samples = [{"a": 1000, "b": 1000}, {"a": 100, "b": 3000}]
        if outage:
            samples.extend(({"b": 3100}, {"a": 150, "b": 3200}))
        samples.append({"a": 150 if outage else 100, "b": 3200 if outage else 3000})
        for index, readings in enumerate(samples):
            await watch.start()
            if outage and "a" not in readings:
                with pytest.raises(PanelUnavailableError):
                    await watch.record_daily_snapshot()
            else:
                await watch.record_daily_snapshot()
            if index == 1:
                rows = await db.get_bandwidth_snapshots("alice", 0)
                assert sum(row["up"] for row in rows) == 2000
            if outage and "a" not in readings:
                assert watch.snap_mem["a", "alice", USER_UUID].upload == 1000
                assert watch._snap_reset_mem["a", "alice", USER_UUID].upload == 100
            await watch.close()
            watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
        rows = await db.get_bandwidth_snapshots("alice", 0)
        expected = 2350 if outage else 2100
        assert sum(row["up"] for row in rows) == expected
        assert sum(row["down"] for row in rows) == expected * 2
        assert sum(row["wl_up"] for row in rows) == expected
        assert sum(row["wl_down"] for row in rows) == expected * 2
    finally:
        await watch.close()
        await close_subscription(sub)
        await db.close()
