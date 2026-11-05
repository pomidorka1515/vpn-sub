from __future__ import annotations

import asyncio
import time
from html import escape
from typing import TYPE_CHECKING, Literal

from app_runtime.tasks import TaskRegistry
from errors import PanelUnavailableError
from loggers import Logger

from .panels import PanelJobs
from .quota import QuotaJobs
from .snapshots import SnapshotJobs
from .usage import UsageJobs

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from config import AppConfig
    from core import Subscription
    from core.common import Snapshot
    from db import Database
    from notify.notifier import Notifier

    from ._base import Counters


class BWatch(UsageJobs, QuotaJobs, SnapshotJobs, PanelJobs):
    """Runtime-owned jobs. Public job methods share locks with scheduled calls."""

    _INTERVALS: tuple[tuple[str, float, tuple[tuple[str, str], ...]], ...] = (
        ("Bandwidth", 15, (("bandwidth poll", "bandwidth_check"),)),
        ("Quota & Notifs", 120, (("periodic user check", "check"),)),
        ("Panels check", 300, (("panel health check", "panel_health_check"),)),
        (
            "Reconcile",
            7200,
            (("inbound reconcile", "reconcile_inbounds"),),
        ),
        (
            "Snapshots",
            86400,
            (
                ("bandwidth snapshot pruning", "prune_old_bw_snapshots"),
                ("state snapshot pruning", "prune_old_snap_snapshots"),
            ),
        ),
    )

    def __init__(
        self,
        cfg: Snapshot[AppConfig],
        db: Database,
        sub: Subscription,
        *,
        notifier: Notifier | None = None,
        alert: Callable[[str], Awaitable[object]] | None = None,
        scheduled: bool = True,
        verbose: bool = False,
    ) -> None:
        self.cfg, self.db, self.sub = cfg, db, sub
        self.notifier, self.alert = notifier, alert
        self.scheduled, self.verbose = scheduled, verbose
        self.log = Logger(type(self).__name__)
        self.mem: Counters = {}
        self.wl_mem: Counters = {}
        self._reset_mem: Counters = {}
        self._reset_wl_mem: Counters = {}
        self._usage_month: str | None = None
        self.snap_mem: Counters = {}
        self.snap_wl_mem: Counters = {}
        self._snap_reset_mem: Counters = {}
        self._snap_reset_wl_mem: Counters = {}
        self._tasks = TaskRegistry()
        self._jobs: dict[str, asyncio.Lock] = {}
        self._started = False
        self._closed = False
        self._snapshot_failures: dict[Literal["bandwidth", "state"], int] = {}
        self._snapshot_due_at: dict[Literal["bandwidth", "state"], float] = {
            "bandwidth": 0,
            "state": 0,
        }
        self._failure: dict[str, int] | None = None
        self._panel_alerts: dict[str, float] = {}

    async def start(self) -> None:
        if self._started:
            return
        if self._closed:
            raise RuntimeError("watcher is closed")
        self._failure = await self._load_failure()
        states = await self.sub.user_svc.list_user_states()
        await self._load_usage_baselines(states)
        has_snapshot_baselines = await self._load_snapshot_baselines(states)
        if not self.scheduled:
            self._started = True
            return
        try:
            await self.bandwidth_check()
        except PanelUnavailableError:
            self.log.exception("initial bandwidth poll failed")
        if not has_snapshot_baselines:
            # First run/upgrade: historical daily baselines cannot be recovered.
            self.snap_mem = dict(self.mem)
            self.snap_wl_mem = dict(self.wl_mem)
        await self._run_due_daily_snapshots(time.monotonic())
        for name, interval, jobs in self._INTERVALS:

            async def loop(
                interval: float = interval,
                jobs: tuple[tuple[str, str], ...] = jobs,
            ) -> None:
                await self._loop(interval, jobs)

            self._tasks.create(loop, name=f"watcher:{name}")
        self._tasks.create(self._daily_loop, name="watcher:daily snapshots")
        self._started = True

    async def close(self) -> None:
        self._closed = True
        await self._tasks.drain(cancel=True)

    async def stop(self) -> None:
        await self.close()

    async def _job(self, name: str, operation: Callable[[], Awaitable[None]]) -> None:
        if not self.sub.panels and name in {
            "quota",
            "bandwidth",
            "daily bandwidth",
            "reconcile",
            "health",
        }:
            return
        async with self._jobs.setdefault(name, asyncio.Lock()):
            if self._closed:
                return
            await operation()

    async def _loop(self, interval: float, jobs: tuple[tuple[str, str], ...]) -> None:
        while True:
            await asyncio.sleep(interval)
            for label, name in jobs:
                try:
                    await getattr(self, name)()
                except Exception:
                    self.log.exception("%s crashed", label)

    async def _alert_admin(self, message: str) -> None:
        if self.alert is not None:
            try:
                # Watcher alerts are plain text; the admin sender uses HTML parsing.
                await self.alert(escape(message))
            except Exception:
                self.log.exception("watcher alert failed")

    async def _run_due_daily_snapshots(self, now: float) -> None:
        for kind in self._snapshot_due_at:
            if self._snapshot_due_at[kind] > now:
                continue
            try:
                await (
                    self.record_daily_snapshot()
                    if kind == "bandwidth"
                    else self.record_snap_snapshot()
                )
                failures = self._snapshot_failures.pop(kind, 0)
                if failures:
                    self.log.info(
                        "Daily %s snapshot recovered after %d failed attempts",
                        kind,
                        failures,
                    )
                # Re-align after every run, including startup and delayed retries.
                # A fixed 24h delay would preserve startup time and accumulate drift.
                delay = 86400 - time.time() % 86400
            except Exception as exc:
                failures = self._snapshot_failures.get(kind, 0) + 1
                self._snapshot_failures[kind] = failures
                self.log.exception("Daily %s snapshot failed", kind)
                if failures == 1 or failures % 3 == 0:
                    await self._alert_admin(
                        f"⚠️ Daily {kind} snapshot failed ({failures} consecutive attempt(s)): {exc}",
                    )
                delay = min(3600.0 * failures, 86400.0)
            self._snapshot_due_at[kind] = time.monotonic() + delay

    async def _daily_loop(self) -> None:
        while True:
            await asyncio.sleep(max(0, min(self._snapshot_due_at.values()) - time.monotonic()))
            await self._run_due_daily_snapshots(time.monotonic())
