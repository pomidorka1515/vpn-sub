from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ._base import WatchJobs

if TYPE_CHECKING:
    from db.state import UsageBaselineKey, UsageBaselines

    from ._base import Counters


class UsageJobs(WatchJobs):
    """Durable per-panel bandwidth accounting and monthly rollover."""

    def _init_usage(self) -> None:
        self.mem, self.wl_mem = {}, {}
        self._reset_mem, self._reset_wl_mem = {}, {}
        self._usage_month = None
        self._usage_ready = False
        self._usage_known: dict[str, str] = {}
        self._usage_main: set[str] = set()
        self._usage_wl: set[str] = set()
        self._usage_keys: dict[str, set[UsageBaselineKey]] = {}
        self._usage_stale: set[UsageBaselineKey] = set()

    def _usage_counters(self) -> dict[str, Counters]:
        return {
            "main": self.mem,
            "whitelist": self.wl_mem,
            "main_reset": self._reset_mem,
            "whitelist_reset": self._reset_wl_mem,
        }

    def _clear_usage_counters(self) -> None:
        for counters in self._usage_counters().values():
            counters.clear()
        self._usage_keys.clear()
        self._usage_stale.clear()

    def _apply_usage_baselines(
        self, changed: UsageBaselines, removed: set[UsageBaselineKey]
    ) -> None:
        targets = self._usage_counters()
        for kind, panel, name, uuid in removed:
            targets[kind].pop((panel, name, uuid), None)
            if (keys := self._usage_keys.get(name)) is not None:
                keys.discard((kind, panel, name, uuid))
                if not keys:
                    del self._usage_keys[name]
        for (kind, panel, name, uuid), value in changed.items():
            targets[kind][panel, name, uuid] = value
            self._usage_keys.setdefault(name, set()).add((kind, panel, name, uuid))

    async def _refresh_usage_cache(self, reset_month: str | None, *, full: bool = False) -> None:
        full = full or not self._usage_ready
        # Draining the dirty set is a commit too. If interrupted before the cache
        # reflects it, the next poll must rebuild instead of losing invalidations.
        self._usage_ready = False
        dirty = await self.db.take_usage_changes()
        if full or dirty:
            if full:
                states = await self.db.list_sweep_users(active=True)
                saved_month, saved = await self.db.load_usage_baselines()
                self._usage_month = saved_month or reset_month
                self._clear_usage_counters()
                self._usage_known.clear()
                self._usage_main.clear()
                self._usage_wl.clear()
            else:
                states = await self.db.list_sweep_users_for(dirty)
                _, saved = await self.db.load_usage_baselines(dirty)
                self._apply_usage_baselines(
                    {}, {key for name in dirty for key in self._usage_keys.get(name, ())}
                )
                self._usage_main.difference_update(dirty)
                self._usage_wl.difference_update(dirty)
                for name in dirty:
                    self._usage_known.pop(name, None)
            for state in states:
                name = state["username"]
                if state["bw_limit_gb"]:
                    self._usage_main.add(name)
                if state["wl_limit_gb"]:
                    self._usage_wl.add(name)
                if name in self._usage_main or name in self._usage_wl:
                    self._usage_known[name] = state["uuid"]
            accepted: UsageBaselines = {}
            for key, value in saved.items():
                kind, _, name, uuid = key
                metered = self._usage_main if kind.startswith("main") else self._usage_wl
                if name in metered and self._usage_known[name] == uuid:
                    accepted[key] = value
                    self._usage_stale.discard(key)
                else:
                    self._usage_stale.add(key)
            self._apply_usage_baselines(accepted, set())
        self._usage_ready = True

    async def bandwidth_check(self) -> None:
        async def operation() -> None:
            sampled_at = datetime.now(UTC)
            month = sampled_at.strftime("%Y-%m")
            reset_month = await self.db.get_metadata("last_reset_month")
            rollover = reset_month is not None and reset_month != month
            await self._refresh_usage_cache(reset_month, full=rollover)
            clear = self._usage_month is not None and self._usage_month != month
            known = self._usage_known
            changes: UsageBaselines = {}
            removals: set[UsageBaselineKey] = set()
            deltas: dict[str, list[int]] = {}

            for index, (kind, metered, baseline, pending) in enumerate(
                (
                    ("main", self._usage_main, self.mem, self._reset_mem),
                    ("whitelist", self._usage_wl, self.wl_mem, self._reset_wl_mem),
                )
            ):
                if not metered:
                    continue
                values = await self.sub.bandwidth_svc.panel_traffic(
                    whitelist=index == 1, work="background", known=known
                )
                reset_kind = kind + "_reset"
                if not clear:
                    # Missing samples retain the baseline, but interrupt a reset
                    # confirmation. Only outstanding reset keys need this check.
                    removals.update(
                        (reset_kind, panel, name, uuid)
                        for panel, name, uuid in pending
                        if name not in values.get(panel, {})
                    )
                for panel, readings in values.items():
                    for name, current in readings.items():
                        if name not in metered:
                            continue
                        key = (panel, name, known[name])
                        previous = None if clear else baseline.get(key)
                        candidate = None if clear else pending.get(key)
                        delta = 0
                        if previous is None or current.total >= previous.total:
                            delta = int(current.total - previous.total) if previous else 0
                        elif candidate is not None and current.total >= candidate.total:
                            # Include all traffic since the reset, even bytes from
                            # before the first low reading.
                            delta = int(current.total)
                        else:
                            if current != candidate:
                                changes[reset_kind, *key] = current
                            continue
                        if current != previous:
                            changes[kind, *key] = current
                        if candidate is not None:
                            removals.add((reset_kind, *key))
                        if delta:
                            deltas.setdefault(name, [0, 0])[index] += delta

            candidates = {key[2] for key in changes.keys() | removals}
            period = (
                (month, sampled_at.strftime("%Y-%m-%d"))
                if reset_month != month or self._usage_month != month
                else None
            )

            async def commit() -> None:
                # Cumulative counters cannot be attributed across midnight.
                if datetime.now(UTC).strftime("%Y-%m") != month:
                    return
                # A UUID or metering change during panel I/O stays dirty until
                # the next poll. This also catches change-away-and-back races.
                valid = {
                    state["username"]
                    for state in await self.db.list_sweep_users_for(candidates, clean_usage=True)
                    if known.get(state["username"]) == state["uuid"]
                }
                baselines = {key: value for key, value in changes.items() if key[2] in valid}
                removed = {key for key in removals if key[2] in valid} | self._usage_stale
                usage = {
                    name: (delta[0], delta[1]) for name, delta in deltas.items() if name in valid
                }
                try:
                    await self.db.increment_usages(
                        usage,
                        baselines=baselines,
                        removed_baselines=removed,
                        clear_baselines=clear,
                        period=period,
                    )
                except BaseException:
                    # Any failed acknowledgement (including cancellation) can
                    # leave the commit outcome uncertain. SQLite is authoritative.
                    self._usage_ready = False
                    raise
                if clear:
                    self._clear_usage_counters()
                self._apply_usage_baselines(baselines, removed)
                self._usage_stale.clear()
                self._usage_month = month

            if candidates or period is not None or self._usage_stale:
                await self.sub.res.coordinator.run(
                    list(known) if rollover else sorted(candidates), commit, name="watcher:usage"
                )
            await self._recover_quotas()

        await self._job("bandwidth", operation)

    async def is_first(self) -> None:
        """Compatibility entry point: rollover is owned by the usage poll."""
        await self.bandwidth_check()
