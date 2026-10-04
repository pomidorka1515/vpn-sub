from __future__ import annotations

from dataclasses import asdict
import time

from custom_types import BandwidthInfo, UserRecord
from errors import PanelUnavailableError
from sysutil import SysUtil

from .host import BWatchHost


class SnapshotsMixin(BWatchHost):
    """Daily bandwidth and host/panel state snapshots."""

    def prune_old_bw_snapshots(self) -> None:
        retention = int(self.db.get_metadata("bw_retention_days", "30") or 30)
        cutoff = int(time.time()) - retention * 86400
        self.db.prune_bandwidth_snapshots(cutoff)

    def prune_old_snap_snapshots(self) -> None:
        retention = int(self.db.get_metadata("state_retention_days", "30") or 30)
        cutoff = int(time.time()) - retention * 86400
        self.db.prune_state_snapshots(cutoff)

    def record_snap_snapshot(self) -> None:
        """Record one state snapshot (`SysUtil` + panels) for today."""
        panels_data: dict[str, object] = {}
        panel_errors: list[str] = []
        for panel, status in zip(
            self.sub.panels, self.sub.panel_svc.statuses(pool=self._panel_pool()), strict=True
        ):
            if status is None:
                panel_errors.append(panel.name)
            else:
                panels_data[panel.name] = asdict(status.obj)
        if panel_errors:
            raise PanelUnavailableError(
                "state snapshot could not read panel status: " + ", ".join(panel_errors)
            )

        midnight = int(time.time()) - (int(time.time()) % 86400)
        data: dict[str, object] = {
            "ts": midnight,
            "host": asdict(SysUtil.full_info()),
            "panels": panels_data
        }

        self.db.upsert_state_snapshot(midnight, data)

        self.prune_old_snap_snapshots()

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

    def record_daily_snapshot(self) -> None:
        """Record one bandwidth snapshot per user for today (UTC midnight).

        Uses dedicated snapshot baselines so the 15s quota poller cannot shrink
        the day's delta to the last poll interval. Thread-safe: copies snap
        baselines under lock, releases it for panel I/O, then writes rows and
        advances only the counters that were recorded successfully.
        """

        with self._mem_lock:
            mem_snapshot = dict(self.snap_mem)
            wl_mem_snapshot = dict(self.snap_wl_mem)

        midnight = int(time.time()) - (int(time.time()) % 86400)
        # username -> (current, wl_current, up, down, wl_up, wl_down, next_main, next_wl)
        snapshot_data: dict[
            str,
            tuple[
                BandwidthInfo | None,
                BandwidthInfo | None,
                int, int, int, int,
                BandwidthInfo | None,
                BandwidthInfo | None,
            ],
        ] = {}
        eligible_users: list[str] = []
        states: dict[str, UserRecord] = {}
        need_main = False
        need_wl = False
        for state in self.sub.user_svc.list_user_states():
            username = state["username"]
            bw_limit = int(state['bw_limit_gb'])
            wl_limit = int(state['wl_limit_gb'])
            if bw_limit == 0 and wl_limit == 0:
                continue
            eligible_users.append(username)
            states[username] = state
            need_main = need_main or bw_limit != 0
            need_wl = need_wl or wl_limit != 0

        # One batched read per side; per-user deltas come from the maps.
        main_map: dict[str, BandwidthInfo] = {}
        wl_map: dict[str, BandwidthInfo] = {}
        try:
            pool = self._panel_pool()
            if need_main:
                main_map = self.sub.bandwidth_svc.all_traffic(pool=pool)
            if need_wl:
                wl_map = self.sub.bandwidth_svc.all_traffic(whitelist=True, pool=pool)
        except Exception:
            self.log.error(
                "failed to record daily bandwidth snapshot",
                exc_info=True,
            )
            # No rows, no baseline advance — every eligible user counts as
            # failed so the metadata marker and the admin alert still fire.
            if eligible_users:
                self.db.set_metadata(
                    "daily_bw_snapshot_failures",
                    f"{int(time.time())}:{len(eligible_users)}:{len(eligible_users)}",
                )
                raise PanelUnavailableError(
                    "Daily bandwidth snapshot failed for "
                    f"{len(eligible_users)}/{len(eligible_users)} eligible user(s)"
                )
            raise

        for username in eligible_users:
            state = states[username]
            bw_limit = int(state['bw_limit_gb'])
            wl_limit = int(state['wl_limit_gb'])
            current: BandwidthInfo | None = None
            wl_current: BandwidthInfo | None = None
            if bw_limit != 0:
                current = main_map.get(username, BandwidthInfo(0, 0, 0))
            if wl_limit != 0:
                wl_current = wl_map.get(username, BandwidthInfo(0, 0, 0))

            up, down, next_main = self._daily_snapshot_delta(current, mem_snapshot.get(username))
            wl_up, wl_down, next_wl = self._daily_snapshot_delta(
                wl_current, wl_mem_snapshot.get(username),
            )
            snapshot_data[username] = (
                current, wl_current, up, down, wl_up, wl_down, next_main, next_wl,
            )

        snapshot_rows: list[tuple[str, int, int, int, int, int]] = []
        with self._mem_lock:
            for username, (
                _current, _wl_current, up, down, wl_up, wl_down, next_main, next_wl,
            ) in snapshot_data.items():
                if next_main is not None:
                    self.snap_mem[username] = next_main
                if next_wl is not None:
                    self.snap_wl_mem[username] = next_wl
                snapshot_rows.append((username, midnight, up, down, wl_up, wl_down))
        self.db.add_bandwidth_snapshots(snapshot_rows)

        self.prune_old_bw_snapshots()
        self.db.delete_metadata("daily_bw_snapshot_failures")

    def get_daily_snapshot_failure(self) -> dict[str, int] | None:
        """Return metadata from the latest bandwidth snapshot attempt."""
        raw = self.db.get_metadata("daily_bw_snapshot_failures")
        if raw is None:
            return None
        timestamp, separator, counts = raw.partition(":")
        if not separator or ":" not in counts:
            return None
        failed, separator, eligible = counts.partition(":")
        if not separator:
            return None
        try:
            return {
                "ts": int(timestamp),
                "failed": int(failed),
                "eligible": int(eligible),
            }
        except ValueError:
            return None
