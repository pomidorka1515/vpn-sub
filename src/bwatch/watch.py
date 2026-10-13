from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from app_runtime.tasks import TaskRegistry
from custom_types import BandwidthInfo
from errors import AppError, PanelUnavailableError
from loggers import Logger
from notify import classify, restored
from sysutil import SysUtil

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from config import AppConfig, JsonDict
    from core import Subscription
    from core.common import Snapshot
    from custom_types import UserRecord
    from db import Database
    from db.state import StateSnapshot
    from notify import Notice
    from notify.notifier import Notifier

from notify.kinds import Kind, Scope


class BWatch:
    """Runtime-owned jobs. Public job methods share locks with scheduled calls."""

    _INTERVALS: tuple[tuple[str, float, tuple[tuple[str, str], ...]], ...] = (
        ("Bandwidth", 15, (("bandwidth poll", "bandwidth_check"),)),
        ("Quota & Notifs", 120, (("periodic user check", "check"),)),
        ("Panels check", 300, (("panel health check", "panel_health_check"),)),
        (
            "Date check & reconcile",
            7200,
            (
                ("monthly reset check", "is_first"),
                ("inbound reconcile", "reconcile_inbounds"),
            ),
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

    @staticmethod
    def _daily_snapshot_delta(
        current: BandwidthInfo | None,
        last: BandwidthInfo | None,
    ) -> tuple[int, int, BandwidthInfo | None]:
        """Return daily up/down deltas and the baseline to store.

        A missing current means that counter was not queried. A drop below the
        stored baseline is treated as a bad 3x-ui reading: write 0 and keep the
        old baseline so a flake cannot become a later spike.
        """
        if current is None:
            return 0, 0, None
        if last is None:
            return 0, 0, current
        if current.upload < last.upload or current.download < last.download:
            return 0, 0, None
        return (
            int(current.upload - last.upload),
            int(current.download - last.download),
            current,
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
        self.mem: dict[str, BandwidthInfo] = {}
        self.wl_mem: dict[str, BandwidthInfo] = {}
        self.snap_mem: dict[str, BandwidthInfo] = {}
        self.snap_wl_mem: dict[str, BandwidthInfo] = {}
        self._snapshot_initialized = False
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
        if not self.scheduled:
            self._started = True
            return
        wl = await self.sub.bandwidth_svc.all_traffic(whitelist=True, work="background")
        main = await self.sub.bandwidth_svc.all_traffic(work="background")
        for state in await self.sub.user_svc.list_user_states():
            name = state["username"]
            self.wl_mem[name] = wl.get(name, BandwidthInfo(0, 0, 0))
            if state["bw_limit_gb"] != 0:
                self.mem[name] = main.get(name, BandwidthInfo(0, 0, 0))
        self.snap_mem, self.snap_wl_mem = dict(self.mem), dict(self.wl_mem)
        self._snapshot_initialized = True
        await self._run_due_daily_snapshots(time.monotonic())
        await self.is_first()
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
                await self.alert(message)
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
                delay = 86400.0
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

    async def _deliver(self, state: UserRecord, notices: tuple[Notice, ...]) -> None:
        if self.notifier is None or not notices:
            return
        # The account coordinator covers read/send/persist, including cancellation.
        seen = await self.db.notification_markers()
        before = set(seen)
        mapped = (await self.db.user_tgids()).get(state["username"])
        for notice in notices:
            await self.notifier.deliver(
                state["username"],
                int(mapped) if mapped is not None else None,
                notice,
                seen,
            )
            await self.db.sync_notifications(before, seen)
            before = set(seen)

    async def _quota(self, username: str, *, recover: bool) -> None:
        async def operation() -> None:
            state = await self.sub.user_svc.get_user_state(username)
            now = int(time.time())
            time_ok = state["expires_at"] == 0 or state["expires_at"] >= now
            wl_ok = state["wl_limit_gb"] == 0 or state["wl_used"] < state["wl_limit_gb"] * 10**9
            # Poll re-enables only; the 120s job handles terminal disables.
            if recover:
                main = (
                    True
                    if state["bw_limit_gb"] != 0
                    and state["bw_used"] < state["bw_limit_gb"] * 10**9
                    and not state["enabled"]
                    else None
                )
                restored_main = main is True
                timing = True if time_ok and not state["enabled_time"] else None
                if timing is not None:
                    restored_main = False
                whitelist = (
                    True
                    if state["wl_limit_gb"] != 0 and wl_ok and not state["enabled_wl"]
                    else None
                )
                if timing is not None and main is None:
                    main = True
                if main is not None or whitelist is not None:
                    await self.sub.business_svc.update_user(
                        username,
                        enable=main,
                        timee=timing,
                        wl_enable=whitelist,
                        work="background",
                    )
                await self._deliver(
                    state,
                    restored(main=restored_main, whitelist=whitelist is True, time=timing is True),
                )
            else:
                notices = classify(state, now)
                applied: list[Notice] = []
                for notice in notices:
                    if notice.kind.scope is Scope.EPISODE:
                        try:
                            if notice.kind is Kind.EXPIRED:
                                await self.sub.business_svc.update_user(
                                    username,
                                    enable=False,
                                    timee=False,
                                    work="background",
                                )
                            elif notice.kind is Kind.TRAFFIC_EXHAUSTED:
                                await self.sub.business_svc.update_user(
                                    username,
                                    enable=False,
                                    timee=True,
                                    work="background",
                                )
                            elif notice.kind is Kind.WHITELIST_EXHAUSTED:
                                await self.sub.business_svc.update_user(
                                    username,
                                    wl_enable=False,
                                    work="background",
                                )
                        except AppError:
                            self.log.exception("background user update failed for %s", username)
                            continue
                    applied.append(notice)
                await self._deliver(state, tuple(applied))

        try:
            await self.sub.res.coordinator.run([username], operation, name="watcher:quota")
        except AppError:
            self.log.exception("background user update failed for %s", username)

    async def check(self) -> None:
        async def operation() -> None:
            for username in await self.sub.user_svc.list_users():
                await self._quota(username, recover=False)

        await self._job("quota", operation)

    async def bandwidth_check(self) -> None:
        async def operation() -> None:
            for username in await self.sub.user_svc.list_users():
                await self._quota(username, recover=True)
            states = await self.sub.user_svc.list_user_states()
            main, wl = await self._traffic(states)
            sampled = {s["username"]: s for s in states}

            async def commit() -> None:
                current_states = await self.sub.user_svc.list_user_states()
                usage: dict[str, tuple[int, int]] = {}
                next_main, next_wl = dict(self.mem), dict(self.wl_mem)
                for state in current_states:
                    name = state["username"]
                    if name not in sampled:
                        continue
                    deltas: list[int] = []
                    for limit, values, baseline in (
                        (state["bw_limit_gb"] and sampled[name]["bw_limit_gb"], main, next_main),
                        (state["wl_limit_gb"] and sampled[name]["wl_limit_gb"], wl, next_wl),
                    ):
                        current = values.get(name, BandwidthInfo(0, 0, 0))
                        delta = (
                            max(0, int(current.total - baseline.get(name, current).total))
                            if limit
                            else 0
                        )
                        deltas.append(delta)
                        if limit and (name not in baseline or delta > 0):
                            baseline[name] = current
                    usage[name] = (deltas[0], deltas[1])
                await self.db.increment_usages(usage)
                self.mem, self.wl_mem = next_main, next_wl

            await self.sub.res.coordinator.run(
                [s["username"] for s in states],
                commit,
                name="watcher:usage",
            )

        await self._job("bandwidth", operation)

    async def _traffic(
        self,
        states: list[UserRecord],
    ) -> tuple[dict[str, BandwidthInfo], dict[str, BandwidthInfo]]:
        main = (
            await self.sub.bandwidth_svc.all_traffic(work="background")
            if any(s["bw_limit_gb"] for s in states)
            else {}
        )
        wl = (
            await self.sub.bandwidth_svc.all_traffic(whitelist=True, work="background")
            if any(s["wl_limit_gb"] for s in states)
            else {}
        )
        return main, wl

    async def record_daily_snapshot(self) -> None:
        async def operation() -> None:
            states = [
                s
                for s in await self.sub.user_svc.list_user_states()
                if s["bw_limit_gb"] or s["wl_limit_gb"]
            ]
            try:
                main, wl = await self._traffic(states)
            except Exception as exc:
                if states:
                    self._failure = {
                        "ts": int(time.time()),
                        "failed": len(states),
                        "eligible": len(states),
                    }
                    await self.db.set_metadata(
                        "daily_bw_snapshot_failures",
                        f"{self._failure['ts']}:{len(states)}:{len(states)}",
                    )
                    raise PanelUnavailableError(
                        f"Daily bandwidth snapshot failed for {len(states)}/{len(states)} eligible user(s)",
                    ) from exc
                raise

            async def commit() -> None:
                next_main, next_wl = dict(self.snap_mem), dict(self.snap_wl_mem)
                rows: list[tuple[str, int, int, int, int, int]] = []
                midnight = int(time.time()) // 86400 * 86400
                for old in states:
                    if not await self.sub.user_svc.isuser(old["username"]):
                        continue
                    state = await self.sub.user_svc.get_user_state(old["username"])
                    name = state["username"]
                    up, down, baseline = self._daily_snapshot_delta(
                        main.get(name, BandwidthInfo(0, 0, 0))
                        if state["bw_limit_gb"] and old["bw_limit_gb"]
                        else None,
                        self.snap_mem.get(name),
                    )
                    wl_up, wl_down, wl_baseline = self._daily_snapshot_delta(
                        wl.get(name, BandwidthInfo(0, 0, 0))
                        if state["wl_limit_gb"] and old["wl_limit_gb"]
                        else None,
                        self.snap_wl_mem.get(name),
                    )
                    rows.append((name, midnight, up, down, wl_up, wl_down))
                    if baseline is not None:
                        next_main[name] = baseline
                    if wl_baseline is not None:
                        next_wl[name] = wl_baseline
                await self.db.add_bandwidth_snapshots(rows)
                self.snap_mem, self.snap_wl_mem = next_main, next_wl
                await self.db.delete_metadata("daily_bw_snapshot_failures")
                self._failure = None

            await self.sub.res.coordinator.run(
                [s["username"] for s in states],
                commit,
                name="watcher:snapshot",
            )
            await self.prune_old_bw_snapshots()

        await self._job("daily bandwidth", operation)

    async def record_snap_snapshot(self) -> None:
        async def operation() -> None:
            panels: dict[str, JsonDict] = {}
            statuses = await self.sub.panel_svc.statuses(work="background")
            for panel, status in zip(self.sub.panels, statuses, strict=True):
                if status is None:
                    raise PanelUnavailableError(
                        f"state snapshot could not read panel status: {panel.name}",
                    )
                panels[panel.name] = asdict(status.obj)
            midnight = int(time.time()) // 86400 * 86400
            data: StateSnapshot = {
                "ts": midnight,
                "host": asdict(await self.sub.res.offloads.metrics.run(SysUtil.full_info)),
                "panels": panels,
            }
            await self.db.upsert_state_snapshot(midnight, data)
            await self.prune_old_snap_snapshots()

        await self._job("daily state", operation)

    async def prune_old_bw_snapshots(self) -> None:
        retention = int(await self.db.get_metadata("bw_retention_days", "30") or 30)
        await self.db.prune_bandwidth_snapshots(int(time.time()) - retention * 86400)

    async def prune_old_snap_snapshots(self) -> None:
        retention = int(await self.db.get_metadata("state_retention_days", "30") or 30)
        await self.db.prune_state_snapshots(int(time.time()) - retention * 86400)

    async def is_first(self) -> None:
        async def operation() -> None:
            now = datetime.now(UTC)
            if now.day == 1:

                async def reset() -> None:
                    await self.db.reset_monthly(now.strftime("%Y-%m"), now.strftime("%Y-%m-%d"))

                await self.sub.res.coordinator.run(
                    await self.sub.user_svc.list_users(),
                    reset,
                    name="watcher:monthly reset",
                )

        await self._job("monthly", operation)

    async def reconcile_inbounds(self) -> None:
        async def operation() -> None:
            failures = await self.sub.business_svc.reconcile()
            if failures:
                await self._alert_admin(
                    f"⚠️ Inbound reconcile failed for {len(failures)} user(s): {', '.join(failures[:10])}",
                )

        await self._job("reconcile", operation)

    async def panel_health_check(self) -> None:
        async def operation() -> None:
            live = [p for p in self.sub.panels if not p.dead]
            for panel, status in zip(
                live,
                await self.sub.panel_svc.statuses(live, work="background"),
                strict=True,
            ):
                if status is None:
                    continue
                obj = status.obj
                obj.format()
                problems: list[str] = []
                if obj.xray.state != "running":
                    problems.append(f"Xray: {obj.xray.state} - {obj.xray.errorMsg}")
                if obj.cpu > 90:
                    problems.append(f"CPU: {obj.cpu}%")
                for label, value in (("RAM", obj.mem), ("Disk", obj.disk)):
                    if value.total and value.current / value.total > 0.9:
                        problems.append(f"{label}: {value.current / value.total * 100:.0f}%")
                if not problems:
                    self._panel_alerts.pop(panel.name, None)
                elif time.monotonic() - self._panel_alerts.get(panel.name, float("-inf")) > (
                    self.cfg.view()["panel_alert_cooldown"] or 3600
                ):
                    await self._alert_admin(
                        f"⚠️ Panel {panel.name}:\n" + "\n".join(f"- {p}" for p in problems),
                    )
                    self._panel_alerts[panel.name] = time.monotonic()

        await self._job("health", operation)

    async def _load_failure(self) -> dict[str, int] | None:
        raw = await self.db.get_metadata("daily_bw_snapshot_failures")
        try:
            ts, failed, eligible = (int(v) for v in (raw or "").split(":"))
            return {"ts": ts, "failed": failed, "eligible": eligible}
        except ValueError:
            return None

    def get_daily_snapshot_failure(self) -> dict[str, int] | None:
        return dict(self._failure) if self._failure is not None else None
