from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from custom_types import BandwidthInfo

from ._base import WatchJobs

if TYPE_CHECKING:
    from collections.abc import Sequence

    from custom_types import SweepUser

    from ._base import CounterKey


class UsageJobs(WatchJobs):
    """Durable per-panel bandwidth accounting and monthly rollover."""

    async def _load_usage_baselines(self, states: Sequence[SweepUser]) -> str | None:
        reset_month = await self.db.get_metadata("last_reset_month")
        self._usage_month = reset_month
        raw_baselines = await self.db.get_metadata("watcher_usage_baselines")
        if raw_baselines is not None:
            saved = json.loads(raw_baselines)
            self._usage_month = saved.get("month", self._usage_month)
            self.mem, self.wl_mem = {}, {}
            self._reset_mem, self._reset_wl_mem = {}, {}
            # Legacy aggregate counters cannot be split reliably between panels.
            # Rebaseline once on upgrade rather than invent usage or reset evidence.
            if saved.get("version") != 2 or self._usage_month is None:
                return reset_month
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
        return reset_month

    async def bandwidth_check(self) -> None:
        async def operation() -> None:
            sampled_at = datetime.now(UTC)
            month = sampled_at.strftime("%Y-%m")
            states = await self.db.list_sweep_users()
            known = {s["username"]: s["uuid"] for s in states}
            # Per-panel reads omit outages so healthy panels and the independent
            # whitelist pool can still commit, roll over, and run recovery checks.
            main = (
                await self.sub.bandwidth_svc.panel_traffic(work="background", known=known)
                if any(s["bw_limit_gb"] for s in states)
                else {}
            )
            wl = (
                await self.sub.bandwidth_svc.panel_traffic(
                    whitelist=True, work="background", known=known
                )
                if any(s["wl_limit_gb"] for s in states)
                else {}
            )
            sampled = {s["username"]: s for s in states}
            # A cancelled commit may have reached SQLite before cancellation.
            reset_month = await self._load_usage_baselines(states)
            rollover = reset_month is not None and reset_month != month
            candidates: set[str] = set()
            if self._usage_month is None or self._usage_month == month:
                for values, baseline, pending, limit in (
                    (main, self.mem, self._reset_mem, False),
                    (wl, self.wl_mem, self._reset_wl_mem, True),
                ):
                    for panel, readings in values.items():
                        for name, current in readings.items():
                            state = sampled.get(name)
                            if state is None or not (
                                state["wl_limit_gb"] if limit else state["bw_limit_gb"]
                            ):
                                continue
                            key = (panel, name, state["uuid"])
                            previous = baseline.get(key)
                            candidate = pending.get(key)
                            if previous is not None and (
                                current.total > previous.total
                                or (
                                    current.total < previous.total
                                    and candidate is not None
                                    and current.total >= candidate.total
                                    and current.total > 0
                                )
                            ):
                                candidates.add(name)

            recovery_states: list[SweepUser] = states

            async def commit() -> None:
                nonlocal recovery_states
                # A poll spanning midnight has no reliable monthly attribution.
                # Leave its baselines untouched and rebaseline on the next poll.
                if datetime.now(UTC).strftime("%Y-%m") != month:
                    return
                if rollover:
                    # Rollover changes every account, so serialize it against all
                    # sampled users and refresh their state before recovery.
                    current_states = await self.db.list_sweep_users()
                else:
                    current_states = list(states)
                    refreshed = {
                        state["username"]: state
                        for state in await self.db.list_sweep_users_for(candidates)
                    }
                    for name in candidates:
                        current_states[next_index[name]] = refreshed.get(
                            name, {**sampled[name], "uuid": ""}
                        )
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
                    if any(deltas):
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
                for state in current_states:
                    if rollover:
                        state["bw_used"] = state["wl_used"] = 0
                    regular, whitelist = usage.get(state["username"], (0, 0))
                    state["bw_used"] += regular
                    state["wl_used"] += whitelist
                recovery_states = current_states

            next_index = {state["username"]: index for index, state in enumerate(states)}
            await self.sub.res.coordinator.run(
                list(known) if rollover else sorted(candidates),
                commit,
                name="watcher:usage",
            )
            await self._recover_quotas(recovery_states)

        await self._job("bandwidth", operation)

    async def is_first(self) -> None:
        """Compatibility entry point: rollover is owned by the usage poll."""
        await self.bandwidth_check()
