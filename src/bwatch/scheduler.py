from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import Executor
from typing import Literal

from .host import BWatchHost


class SchedulerMixin(BWatchHost):
    """Interval loops and daily snapshot retry."""

    # (thread name, seconds, ((log label, method name), ...))
    # Daily snapshots are not here: they retry per kind with backoff.
    _INTERVALS: tuple[tuple[str, float, tuple[tuple[str, str], ...]], ...] = (
        ("Bandwidth", 15, (("bandwidth poll", "bandwidth_check"),)),
        ("Quota & Notifs", 120, (("periodic user check", "check"),)),
        ("Panels check", 300, (("panel health check", "panel_health_check"),)),
        ("Date check & reconcile", 7200, (
            ("monthly reset check", "is_first"),
            ("inbound reconcile", "reconcile_inbounds"),
        )),
        ("Snapshots", 86400, (
            ("bandwidth snapshot pruning", "prune_old_bw_snapshots"),
            ("state snapshot pruning", "prune_old_snap_snapshots"),
        )),
    )

    @staticmethod
    def _panel_pool() -> Executor:
        """Background panel executor. Imported lazily: app imports BWatch
        before ``core`` has finished loading ``panel``.
        """
        from core.services.panel import BG_POOL
        return BG_POOL

    def _alert_admin(self, message: str) -> None:
        if self.admin_bot:
            self.admin_bot.msg(message)

    def _guarded(self, what: str, operation: Callable[[], object]) -> None:
        """Run one periodic operation; a crash must not kill the loop thread."""
        try:
            operation()
        except Exception:
            self.log.exception("%s crashed", what)

    def _run_jobs(self, jobs: tuple[tuple[str, str], ...]) -> None:
        for label, name in jobs:
            operation = getattr(self, name)
            self._guarded(label, operation)

    def _loop(self, interval: float, jobs: tuple[tuple[str, str], ...]) -> None:
        while not self._stop_event.wait(interval):
            self._run_jobs(jobs)

    def _run_daily_snapshot(
        self,
        kind: Literal["bandwidth", "state"],
        operation: Callable[[], object],
    ) -> float:
        try:
            operation()
            failures = self._snapshot_failures.pop(kind, 0)
            if failures:
                self.log.info("Daily %s snapshot recovered after %d failed attempts", kind, failures)
            return 86400.0
        except Exception as exc:
            failures = self._snapshot_failures.get(kind, 0) + 1
            self._snapshot_failures[kind] = failures
            self.log.error("Daily %s snapshot failed", kind, exc_info=exc)
            if failures == 1 or failures % 3 == 0:
                self._alert_admin(
                    f"⚠️ Daily {kind} snapshot failed ({failures} consecutive attempt(s)): {exc}"
                )
            return min(3600.0 * failures, 86400.0)

    def _run_due_daily_snapshots(self, now: float) -> None:
        due_snapshots: tuple[Literal["bandwidth", "state"], ...] = tuple(
            kind for kind, due_at in self._snapshot_due_at.items() if due_at <= now
        )
        for kind in due_snapshots:
            operation = (
                self.record_daily_snapshot
                if kind == "bandwidth"
                else self.record_snap_snapshot
            )
            delay = self._run_daily_snapshot(kind, operation)
            self._snapshot_due_at[kind] = now + delay

    def _every_24h_snapshot(self) -> None:
        while True:
            now = time.monotonic()
            self._run_due_daily_snapshots(now)
            next_due = min(self._snapshot_due_at.values())
            if next_due <= now:
                continue
            next_delay = next_due - now
            if self._stop_event.wait(next_delay):
                return
