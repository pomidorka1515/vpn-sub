from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING, Literal, cast

from app_runtime.tasks import TaskRegistry
from custom_types import BandwidthInfo
from errors import AppError, PanelUnavailableError
from loggers import Logger
from notify import Notice, classify, restored
from sysutil import SysUtil

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from config import AppConfig, JsonDict
    from core import Subscription
    from core.common import Snapshot
    from custom_types import UserRecord
    from db import Database
    from db.state import StateSnapshot
    from notify.notifier import Notifier

from notify.kinds import Kind, Scope

type CounterKey = tuple[str, str, str]  # panel, username, account UUID
type Counters = dict[CounterKey, BandwidthInfo]


class BWatch:
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

    @staticmethod
    def _daily_snapshot_delta(
        current: BandwidthInfo | None,
        last: BandwidthInfo | None,
        pending: BandwidthInfo | None = None,
    ) -> tuple[int, int, BandwidthInfo | None, BandwidthInfo | None]:
        """Return daily deltas, the baseline, and unconfirmed reset evidence.

        Require two nondecreasing low readings to confirm a reset. Keep the old
        baseline for a single bad reading, but count post-reset traffic once
        confirmed, including traffic present in the first low reading.
        """
        if current is None:
            return 0, 0, None, None
        if last is None:
            return 0, 0, current, None
        if current.upload < last.upload or current.download < last.download:
            if (
                pending is None
                or current.upload < pending.upload
                or current.download < pending.download
            ):
                return 0, 0, None, current
            return (
                int(
                    current.upload if current.upload < last.upload else current.upload - last.upload
                ),
                int(
                    current.download
                    if current.download < last.download
                    else current.download - last.download,
                ),
                current,
                None,
            )
        return (
            int(current.upload - last.upload),
            int(current.download - last.download),
            current,
            None,
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

    async def _load_usage_baselines(self, states: list[UserRecord]) -> None:
        self._usage_month = await self.db.get_metadata("last_reset_month")
        raw_baselines = await self.db.get_metadata("watcher_usage_baselines")
        if raw_baselines is not None:
            saved = json.loads(raw_baselines)
            self._usage_month = saved.get("month", self._usage_month)
            self.mem, self.wl_mem = {}, {}
            self._reset_mem, self._reset_wl_mem = {}, {}
            # Legacy aggregate counters cannot be split reliably between panels.
            # Rebaseline once on upgrade rather than invent usage or reset evidence.
            if saved.get("version") != 2 or self._usage_month is None:
                return
            identities = {state["username"]: state["uuid"] for state in states}
            records = cast(dict[str, list[tuple[str, str, str, int, int, int]]], saved["counters"])
            for kind, target in (
                ("main", self.mem),
                ("whitelist", self.wl_mem),
                ("main_reset", self._reset_mem),
                ("whitelist_reset", self._reset_wl_mem),
            ):
                for panel, name, uuid, up, down, total in records.get(kind, []):
                    if identities.get(name) == uuid:
                        target[panel, name, uuid] = BandwidthInfo(up, down, total)

    async def _load_snapshot_baselines(self, states: list[UserRecord]) -> bool:
        raw = await self.db.get_metadata("watcher_snapshot_baselines")
        if raw is None:
            return False
        saved = json.loads(raw)
        identities = {state["username"]: state["uuid"] for state in states}
        self.snap_mem, self.snap_wl_mem = {}, {}
        self._snap_reset_mem, self._snap_reset_wl_mem = {}, {}
        # Aggregate baselines cannot recover independent panel reset domains.
        if saved.get("version") != 2:
            return False
        records = cast(dict[str, list[tuple[str, str, str, int, int, int]]], saved["counters"])
        for kind, target in (
            ("main", self.snap_mem),
            ("whitelist", self.snap_wl_mem),
            ("main_reset", self._snap_reset_mem),
            ("whitelist_reset", self._snap_reset_wl_mem),
        ):
            for panel, name, uuid, up, down, total in records.get(kind, []):
                if identities.get(name) == uuid:
                    target[panel, name, uuid] = BandwidthInfo(up, down, total)
        return True

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

    async def _deliver(self, state: UserRecord, notices: tuple[Notice, ...]) -> None:
        if self.notifier is None:
            return
        # The account coordinator covers read/send/persist, including cancellation.
        storage_key = f"user_notifications_pending:{state['username']}"
        raw = await self.db.get_metadata(storage_key)
        if raw is None and not notices:
            return
        seen = await self.db.notification_markers()
        before = set(seen)
        pending: dict[str, Notice] = {}
        if raw is not None:
            saved = json.loads(raw)
            if saved.get("uuid") == state["uuid"]:
                for item in saved["notices"]:
                    notice = Notice(
                        Kind[item["kind"]],
                        item["bucket"],
                        item["fields"],
                        tuple(item["clears"]),
                    )
                    pending[notice.marker] = notice
        for notice in notices:
            # A newer transition supersedes undelivered news about the old state.
            clears = set(notice.clears)
            if notice.kind.opens_recovery is not None:
                clears.add(notice.kind.opens_recovery.kind_id)
            pending = {
                key: value for key, value in pending.items() if value.kind.kind_id not in clears
            }
            if notice.kind.scope is not Scope.THRESHOLD:
                pending[notice.marker] = notice
        pending = {
            key: notice
            for key, notice in pending.items()
            if self.notifier.pending(state["username"], notice, seen)
        }

        async def persist() -> None:
            if pending:
                await self.db.set_metadata(
                    storage_key,
                    json.dumps(
                        {
                            "uuid": state["uuid"],
                            "notices": [
                                {**asdict(notice), "kind": notice.kind.name}
                                for notice in pending.values()
                            ],
                        }
                    ),
                )
            else:
                await self.db.delete_metadata(storage_key)

        # Save intent before attempting Telegram; successful markers remain separate.
        await persist()
        mapped = (await self.db.user_tgids()).get(state["username"])
        candidates = (*pending.values(), *(n for n in notices if n.kind.scope is Scope.THRESHOLD))
        for notice in candidates:
            await self.notifier.deliver(
                state["username"],
                int(mapped) if mapped is not None else None,
                notice,
                seen,
            )
            await self.db.sync_notifications(before, seen)
            before = set(seen)
            if not self.notifier.pending(state["username"], notice, seen):
                pending.pop(notice.marker, None)
                await persist()

    async def _quota(self, username: str, *, recover: bool) -> None:
        async def operation() -> None:
            state = await self.sub.user_svc.get_user_state(username)
            now = int(time.time())
            time_ok = state["expires_at"] == 0 or state["expires_at"] > now
            main_ok = state["bw_limit_gb"] == 0 or state["bw_used"] < state["bw_limit_gb"] * 10**9
            wl_ok = state["wl_limit_gb"] == 0 or state["wl_used"] < state["wl_limit_gb"] * 10**9
            # Poll re-enables only; the 120s job handles terminal disables.
            if recover:
                main = (
                    True
                    if time_ok and main_ok and not state["enabled"] and not state["admin_disabled"]
                    else None
                )
                restored_main = main is True
                timing = True if time_ok and not state["enabled_time"] else None
                if timing is not None:
                    restored_main = False
                whitelist = (
                    True
                    if wl_ok and not state["enabled_wl"] and not state["admin_disabled_wl"]
                    else None
                )
                if timing is not None and main is None:
                    main = main_ok and not state["admin_disabled"]
                if main is not None or whitelist is not None:
                    await self.sub.business_svc.update_user(
                        username,
                        enable=main,
                        timee=timing,
                        wl_enable=whitelist,
                        quota=True,
                        work="background",
                    )
                await self._deliver(
                    state,
                    restored(main=restored_main, whitelist=whitelist is True, time=timing is True),
                )
            else:
                notices = classify(state, now)
                terminal = {notice.kind for notice in notices if notice.kind.scope is Scope.EPISODE}
                try:
                    if terminal:
                        disable_main = bool(terminal & {Kind.EXPIRED, Kind.TRAFFIC_EXHAUSTED})
                        # Apply the combined state once: traffic exhaustion must
                        # never overwrite an expired account's time flag.
                        await self.sub.business_svc.update_user(
                            username,
                            enable=False if disable_main else None,
                            timee=time_ok if disable_main else None,
                            wl_enable=False if Kind.WHITELIST_EXHAUSTED in terminal else None,
                            quota=True,
                            work="background",
                        )
                    elif await self.db.get_metadata(f"user_sync_pending:{username}") is not None:
                        # Failed panel writes leave SQLite disabled, so classification
                        # no longer emits a terminal notice. Retry the durable intent
                        # independently of notification eligibility or enabled flags.
                        await self.sub.business_svc.reconcile_user(username)
                except AppError:
                    self.log.exception("background user update failed for %s", username)
                    notices = tuple(notice for notice in notices if notice.kind not in terminal)
                await self._deliver(state, notices)

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
            sampled_at = datetime.now(UTC)
            month = sampled_at.strftime("%Y-%m")
            states = await self.sub.user_svc.list_user_states()
            # Per-panel reads omit outages so healthy panels and the independent
            # whitelist pool can still commit, roll over, and run recovery checks.
            main = (
                await self.sub.bandwidth_svc.panel_traffic(work="background")
                if any(s["bw_limit_gb"] for s in states)
                else {}
            )
            wl = (
                await self.sub.bandwidth_svc.panel_traffic(whitelist=True, work="background")
                if any(s["wl_limit_gb"] for s in states)
                else {}
            )
            sampled = {s["username"]: s for s in states}

            async def commit() -> None:
                # A poll spanning midnight has no reliable monthly attribution.
                # Leave its baselines untouched and rebaseline on the next poll.
                if datetime.now(UTC).strftime("%Y-%m") != month:
                    return
                current_states = await self.sub.user_svc.list_user_states()
                # A cancelled commit may have reached SQLite before cancellation.
                await self._load_usage_baselines(current_states)
                usage: dict[str, tuple[int, int]] = {}
                next_main, next_wl = dict(self.mem), dict(self.wl_mem)
                reset_main, reset_wl = dict(self._reset_mem), dict(self._reset_wl_mem)
                if self._usage_month is not None and self._usage_month != month:
                    # Cumulative counters cannot split cross-month traffic. Drop
                    # old baselines (including missing panels and reset evidence)
                    # instead of billing an outage's traffic to the new month.
                    next_main, next_wl, reset_main, reset_wl = {}, {}, {}, {}
                keys_by_name: dict[str, set[CounterKey]] = {}
                for key in next_main.keys() | next_wl.keys() | reset_main.keys() | reset_wl.keys():
                    keys_by_name.setdefault(key[1], set()).add(key)
                for state in current_states:
                    name = state["username"]
                    if name not in sampled or sampled[name]["uuid"] != state["uuid"]:
                        continue
                    deltas: list[int] = []
                    for limit, values, baseline, pending in (
                        (
                            state["bw_limit_gb"] and sampled[name]["bw_limit_gb"],
                            main,
                            next_main,
                            reset_main,
                        ),
                        (
                            state["wl_limit_gb"] and sampled[name]["wl_limit_gb"],
                            wl,
                            next_wl,
                            reset_wl,
                        ),
                    ):
                        delta = 0
                        # Retain baselines across missing samples, but require two
                        # consecutive low samples to confirm a reset.
                        for key in keys_by_name.get(name, ()):
                            if key[1] == name and (not limit or key[2] != state["uuid"]):
                                baseline.pop(key, None)
                                pending.pop(key, None)
                            elif key[1] == name and values.get(key[0], {}).get(name) is None:
                                pending.pop(key, None)
                        for panel, readings in values.items() if limit else ():
                            current = readings.get(name)
                            if current is None:
                                continue
                            key = (panel, name, state["uuid"])
                            previous = baseline.get(key)
                            if previous is None or current.total >= previous.total:
                                delta += (
                                    int(current.total - previous.total)
                                    if previous is not None
                                    else 0
                                )
                                baseline[key] = current
                                pending.pop(key, None)
                            else:
                                candidate = pending.get(key)
                                if candidate is not None and current.total >= candidate.total:
                                    # The reset zeroed this panel's counter. Include
                                    # traffic already present in the first low read.
                                    delta += int(current.total)
                                    baseline[key] = current
                                    pending.pop(key, None)
                                else:
                                    pending[key] = current
                        deltas.append(delta)
                    usage[name] = (deltas[0], deltas[1])
                identities = {state["username"]: state["uuid"] for state in current_states}
                baselines = json.dumps(
                    {
                        "version": 2,
                        "month": month,
                        "counters": {
                            kind: [
                                [*key, int(value.upload), int(value.download), int(value.total)]
                                for key, value in values.items()
                                if identities.get(key[1]) == key[2]
                            ]
                            for kind, values in (
                                ("main", next_main),
                                ("whitelist", next_wl),
                                ("main_reset", reset_main),
                                ("whitelist_reset", reset_wl),
                            )
                        },
                    },
                )
                await self.db.increment_usages(
                    usage,
                    baselines=baselines,
                    period=(month, sampled_at.strftime("%Y-%m-%d")),
                )
                self.mem, self.wl_mem = next_main, next_wl
                self._reset_mem, self._reset_wl_mem = reset_main, reset_wl

            await self.sub.res.coordinator.run(
                [s["username"] for s in states],
                commit,
                name="watcher:usage",
            )
            for username in await self.sub.user_svc.list_users():
                await self._quota(username, recover=True)

        await self._job("bandwidth", operation)

    async def _traffic(
        self,
        states: list[UserRecord],
    ) -> tuple[dict[str, dict[str, BandwidthInfo]], dict[str, dict[str, BandwidthInfo]]]:
        main = (
            await self.sub.bandwidth_svc.panel_traffic(work="background")
            if any(s["bw_limit_gb"] for s in states)
            else {}
        )
        wl = (
            await self.sub.bandwidth_svc.panel_traffic(whitelist=True, work="background")
            if any(s["wl_limit_gb"] for s in states)
            else {}
        )
        return main, wl

    async def record_daily_snapshot(self) -> None:
        async def operation() -> None:
            sampled_at = int(time.time())
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
                current_states = await self.sub.user_svc.list_user_states()
                # SQLite may have committed even if the previous call was cancelled.
                await self._load_snapshot_baselines(current_states)
                next_main, next_wl = dict(self.snap_mem), dict(self.snap_wl_mem)
                reset_main, reset_wl = dict(self._snap_reset_mem), dict(self._snap_reset_wl_mem)
                rows: list[tuple[str, int, int, int, int, int]] = []
                for old in states:
                    if not await self.sub.user_svc.isuser(old["username"]):
                        continue
                    state = await self.sub.user_svc.get_user_state(old["username"])
                    if state["uuid"] != old["uuid"]:
                        continue
                    name = state["username"]
                    deltas: list[int] = []
                    for limit, values, baseline_values, resets in (
                        (state["bw_limit_gb"] and old["bw_limit_gb"], main, next_main, reset_main),
                        (state["wl_limit_gb"] and old["wl_limit_gb"], wl, next_wl, reset_wl),
                    ):
                        up = down = 0
                        keys = {
                            key for key in baseline_values.keys() | resets.keys() if key[1] == name
                        } | {(panel, name, state["uuid"]) for panel in values}
                        for key in keys:
                            if not limit or key[2] != state["uuid"]:
                                baseline_values.pop(key, None)
                                resets.pop(key, None)
                                continue
                            delta_up, delta_down, baseline, pending = self._daily_snapshot_delta(
                                values.get(key[0], {}).get(name),
                                baseline_values.get(key),
                                resets.get(key),
                            )
                            up += delta_up
                            down += delta_down
                            # Missing panels retain baselines but break consecutive
                            # low readings, just like monthly accounting.
                            if baseline is not None:
                                baseline_values[key] = baseline
                            if pending is None:
                                resets.pop(key, None)
                            else:
                                resets[key] = pending
                        deltas.extend((up, down))
                    # These are deltas since the previous successful reading, not
                    # calendar-day totals. Never backdate startup/retry samples.
                    rows.append((name, sampled_at, deltas[0], deltas[1], deltas[2], deltas[3]))
                identities = {state["username"]: state["uuid"] for state in current_states}
                baselines = json.dumps(
                    {
                        "version": 2,
                        "counters": {
                            kind: [
                                [*key, int(value.upload), int(value.download), int(value.total)]
                                for key, value in values.items()
                                if identities.get(key[1]) == key[2]
                            ]
                            for kind, values in (
                                ("main", next_main),
                                ("whitelist", next_wl),
                                ("main_reset", reset_main),
                                ("whitelist_reset", reset_wl),
                            )
                        },
                    },
                )
                await self.db.add_bandwidth_snapshots(rows, baselines=baselines)
                self.snap_mem, self.snap_wl_mem = next_main, next_wl
                self._snap_reset_mem, self._snap_reset_wl_mem = reset_main, reset_wl
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
            sampled_at = int(time.time())
            panels: dict[str, JsonDict] = {}
            statuses = await self.sub.panel_svc.statuses(work="background")
            for panel, status in zip(self.sub.panels, statuses, strict=True):
                if status is None:
                    raise PanelUnavailableError(
                        f"state snapshot could not read panel status: {panel.name}",
                    )
                panels[panel.name] = asdict(status.obj)
            data: StateSnapshot = {
                "ts": sampled_at,
                "host": asdict(await self.sub.res.offloads.metrics.run(SysUtil.full_info)),
                "panels": panels,
            }
            await self.db.upsert_state_snapshot(sampled_at, data)
            await self.prune_old_snap_snapshots()

        await self._job("daily state", operation)

    async def prune_old_bw_snapshots(self) -> None:
        retention = int(await self.db.get_metadata("bw_retention_days", "30") or 30)
        await self.db.prune_bandwidth_snapshots(int(time.time()) - retention * 86400)

    async def prune_old_snap_snapshots(self) -> None:
        retention = int(await self.db.get_metadata("state_retention_days", "30") or 30)
        await self.db.prune_state_snapshots(int(time.time()) - retention * 86400)

    async def is_first(self) -> None:
        """Compatibility entry point: rollover is owned by the usage poll."""
        await self.bandwidth_check()

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
                elif (
                    time.monotonic() - self._panel_alerts.get(panel.name, float("-inf"))
                    > (self.cfg.view()["panel_alert_cooldown"])
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
