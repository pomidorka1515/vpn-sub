from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, cast

from custom_types import BandwidthInfo
from errors import PanelUnavailableError
from sysutil import SysUtil

from ._base import WatchJobs

if TYPE_CHECKING:
    from config import JsonDict
    from custom_types import UserRecord
    from db.state import StateSnapshot


class SnapshotJobs(WatchJobs):
    """Daily bandwidth/state snapshots, baselines, and retention."""

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

    async def _load_failure(self) -> dict[str, int] | None:
        raw = await self.db.get_metadata("daily_bw_snapshot_failures")
        try:
            ts, failed, eligible = (int(v) for v in (raw or "").split(":"))
            return {"ts": ts, "failed": failed, "eligible": eligible}
        except ValueError:
            return None

    def get_daily_snapshot_failure(self) -> dict[str, int] | None:
        return dict(self._failure) if self._failure is not None else None
