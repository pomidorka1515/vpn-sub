from __future__ import annotations

from concurrent.futures import Executor
from dataclasses import asdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Literal
import threading
import time

from custom_types import BandwidthInfo, BandwidthUpdate, UserRecord
from db import Database
from errors import AppError, PanelUnavailableError
from loggers import Logger
from bots import AdminBot, PublicBot
from config import ConfigLike
from util import SysUtil

if TYPE_CHECKING:
    from core import Subscription

__all__ = ["BWatch"]


class BWatch:
    ### Lifecycle ###

    def __init__(
        self,
        cfg: ConfigLike,
        db: Database,
        sub: Subscription,
        bot: PublicBot | None = None,
        admin_bot: AdminBot | None = None
    ):
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg: ConfigLike = cfg
            self.db = db
            self._stop_event = threading.Event()
            self._mem_lock = threading.Lock()  # single lock for all mem/wl_mem access
            self.sub: Subscription = sub
            self.bot: PublicBot | None = bot
            self.admin_bot: AdminBot | None = admin_bot
            self.mem: dict[str, BandwidthInfo] = {}
            self.wl_mem: dict[str, BandwidthInfo] = {}
            self.snap_mem: dict[str, BandwidthInfo] = {}
            self.snap_wl_mem: dict[str, BandwidthInfo] = {}
            self._snapshot_initialized: bool = False
            self._panel_alerts: dict[str, int | float] = {}  # only used by 1 thread, no lock needed yet
            self._panel_alert_cooldown: int = self.cfg.get('panel_alert_cooldown', as_type=int) or 3600
            self._snapshot_failures: dict[Literal["bandwidth", "state"], int] = {}
            self._snapshot_due_at: dict[Literal["bandwidth", "state"], float] = {
                "bandwidth": 0.0,
                "state": 0.0,
            }
            self._threads: tuple[threading.Thread, ...] = tuple(
                threading.Thread(
                    target=self._loop,
                    args=(interval, jobs),
                    name=name,
                    daemon=True,
                )
                for name, interval, jobs in self._INTERVALS
            ) + (
                threading.Thread(
                    target=self._every_24h_snapshot,
                    name="Daily snapshots",
                    daemon=True,
                ),
            )

    def start(self) -> None:
        initial_mem: dict[str, BandwidthInfo] = {}
        initial_wl_mem: dict[str, BandwidthInfo] = {}
        initial_snap_mem: dict[str, BandwidthInfo] = {}
        initial_snap_wl_mem: dict[str, BandwidthInfo] = {}
        # One batched read per map (whitelist + main); per-user seeding from
        # the two maps. Absent clients seed zero baselines, like before.
        pool = self._panel_pool()
        wl_map = self.sub.bandwidth_svc.all_traffic(whitelist=True, pool=pool)
        main_map = self.sub.bandwidth_svc.all_traffic(pool=pool)
        for i in self.sub.user_svc.list_users():
            wl_current = wl_map.get(i, BandwidthInfo(0, 0, 0))
            initial_wl_mem[i] = wl_current
            initial_snap_wl_mem[i] = wl_current
            if self.sub.user_svc.get_user_state(i)['bw_limit_gb'] == 0:
                continue
            current = main_map.get(i, BandwidthInfo(0, 0, 0))
            initial_mem[i] = current
            initial_snap_mem[i] = current

        with self._mem_lock:
            self.mem = initial_mem
            self.wl_mem = initial_wl_mem
            self.snap_mem = initial_snap_mem
            self.snap_wl_mem = initial_snap_wl_mem
            self._snapshot_initialized = True

        # Run first snapshot immediately; the scheduler tracks retries per kind.
        initial_snapshots: tuple[tuple[Literal["bandwidth", "state"], Callable[[], object]], ...] = (
            ("bandwidth", self.record_daily_snapshot),
            ("state", self.record_snap_snapshot),
        )
        now = time.monotonic()
        for kind, operation in initial_snapshots:
            delay = self._run_daily_snapshot(kind, operation)
            self._snapshot_due_at[kind] = now + delay
        self.is_first()   # also run the monthly reset check immediately

        ### Start Threads ###

        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=5)

    ### Quota and expiry ###

    def _update_user(self, *args: Any, **kwargs: Any) -> bool:
        """Run a background user update. Returns False when it failed.

        Callers must not notify the user of a state change that never
        happened — the next cycle retries it.
        """
        try:
            self.sub.business_svc.update_user(*args, **kwargs)
            return True
        except AppError:
            self.log.error("background user update failed", exc_info=True)
            return False

    def bandwidth_check(self) -> None:
        updates: dict[str, BandwidthUpdate] = {}    # username -> (delta, current) for main
        wl_updates: dict[str, BandwidthUpdate] = {} # username -> (delta, current) for whitelist
        states: dict[str, UserRecord] = {}
        need_main = False
        need_wl = False
        for state in self.sub.user_svc.list_user_states():
            i = state["username"]
            states[i] = state
            # Main bandwidth
            expires_at = int(state['expires_at'])
            time_ok = expires_at == 0 or (expires_at - int(time.time())) >= 0
            if time_ok and not bool(state['enabled_time']):
                self._update_user(username=i, enable=True, timee=True)
                state = self.sub.user_svc.get_user_state(i)  # re-read after the mutation
                states[i] = state

            main_required = int(state['bw_limit_gb']) != 0
            wl_required = int(state['wl_limit_gb']) != 0
            need_main = need_main or main_required
            need_wl = need_wl or wl_required
            if main_required and int(state['bw_used']) < int(int(state['bw_limit_gb']) * 10**9) and not bool(state['enabled']):
                self._update_user(username=i, enable=True)
            if wl_required and int(state['wl_used']) < int(int(state['wl_limit_gb']) * 10**9) and not bool(state['enabled_wl']):
                self._update_user(username=i, wl_enable=True)

        # One batched read per side (clients/list per panel). Read both
        # before committing either: if either required read fails, commit
        # nothing this cycle so no partial delta is recorded.
        main_map: dict[str, BandwidthInfo] = {}
        wl_map: dict[str, BandwidthInfo] = {}
        try:
            pool = self._panel_pool()
            if need_main:
                main_map = self.sub.bandwidth_svc.all_traffic(pool=pool)
            if need_wl:
                wl_map = self.sub.bandwidth_svc.all_traffic(whitelist=True, pool=pool)
        except Exception:
            self.log.error("bandwidth poll failed", exc_info=True)
            return

        with self._mem_lock:
            for i, state in states.items():
                main_required = int(state['bw_limit_gb']) != 0
                wl_required = int(state['wl_limit_gb']) != 0
                if main_required:
                    current_bws = main_map.get(i, BandwidthInfo(0, 0, 0))
                    if i not in self.mem:
                        self.mem[i] = current_bws
                    else:
                        delta = int(current_bws.total - self.mem[i].total)
                        if delta > 0:
                            updates[i] = BandwidthUpdate(delta=delta, current=current_bws)
                if wl_required:
                    current_wl_bws = wl_map.get(i, BandwidthInfo(0, 0, 0))
                    if i not in self.wl_mem:
                        self.wl_mem[i] = current_wl_bws
                    else:
                        delta = int(current_wl_bws.total - self.wl_mem[i].total)
                        if delta > 0:
                            wl_updates[i] = BandwidthUpdate(delta=delta, current=current_wl_bws)

        if not updates and not wl_updates:
            return

        usage: dict[str, tuple[int, int]] = {}
        with self._mem_lock:
            for i, update in updates.items():
                usage[i] = (update.delta, 0)
                self.mem[i] = update.current
            for i, update in wl_updates.items():
                regular, _whitelist = usage.get(i, (0, 0))
                usage[i] = (regular, update.delta)
                self.wl_mem[i] = update.current
        self.db.increment_usages(usage)

    def check(self) -> None:
        tgids = self.db.user_tgids()
        for state in self.sub.user_svc.list_user_states():
            i = state["username"]
            mapped = tgids.get(i)
            tg_user = int(mapped) if mapped is not None else None
            expires_at = int(state['expires_at'])
            bw_limit = int(state['bw_limit_gb'])
            bw_used = int(state['bw_used'])
            wl_limit = int(state['wl_limit_gb'])
            wl_used = int(state['wl_used'])
            if expires_at != 0:
                if (expires_at - int(time.time())) <= 0:
                    if bool(state['enabled_time']):
                        disabled = self._update_user(username=i, enable=False, timee=False)
                        if disabled and self.bot: self.bot.msg(tg_user, 'warning_disabled') # sub expired
                    continue
                else:
                    days = (expires_at - int(time.time())) // 86400
                    if days <= 2 and tg_user is not None and self.db.mark_notification("regular", tg_user):
                            if self.bot: self.bot.msg(tg_user, 'warning_days', days=days)
            if wl_limit != 0 and wl_used > int(wl_limit * 10**9):
                if bool(state['enabled_wl']):
                    disabled = self._update_user(username=i, wl_enable=False)
                    if disabled and self.bot: self.bot.msg(tg_user, 'warning_traffic_whitelist_disabled', available=wl_limit)
            elif wl_limit != 0 and wl_used > int(wl_limit * 10**9 * 0.95) and tg_user is not None and self.db.mark_notification("whitelist", tg_user):
                    if self.bot: self.bot.msg(tg_user, 'warning_traffic_whitelist', used=int(round(wl_used / 10**6, 0)), available=wl_limit)
            if not bool(state['enabled']):
                continue
            if bw_limit == 0:
                continue
            if bw_used > int(bw_limit * 10**9):
                disabled = self._update_user(username=i, enable=False, timee=True)
                if disabled and self.bot: self.bot.msg(tg_user, 'warning_traffic_disabled', available=bw_limit)
            elif bw_used > int(bw_limit * 10**9 * 0.95) and tg_user is not None and self.db.mark_notification("regular", tg_user):
                    if self.bot: self.bot.msg(tg_user, 'warning_traffic', used=int(round(bw_used / 10**6, 0)), available=bw_limit)

    ### Panels ###

    def panel_health_check(self) -> None:
        """Check each panel's Xray status and resource usage. Alert on issues."""
        live = [panel for panel in self.sub.panels if not panel.dead]
        # Dead panels stay out so they do not pick up getstatus's error log.
        # Alert math stays on this thread; _panel_alerts is single-threaded.
        for panel, status in zip(
            live, self.sub.panel_svc.statuses(live, self._panel_pool()), strict=True
        ):
            try:
                if not status:
                    continue
                
                key = panel.name
                problems: list[str] = []
                obj = status.obj
                obj.format()

                xray = obj.xray
                if xray.state != 'running':
                    problems.append(f"Xray: {xray.state} - {xray.errorMsg}")
                
                cpu = obj.cpu
                if cpu > 90:
                    problems.append(f"CPU: {cpu}%")
                
                mem = obj.mem
                if mem.total > 0:
                    mem_pct = (mem.current / mem.total) * 100
                    if mem_pct > 90:
                        problems.append(f"RAM: {mem_pct:.0f}%")

                disk = obj.disk
                if disk.total > 0:
                    disk_pct = (disk.current / disk.total) * 100
                    if disk_pct > 90:
                        problems.append(f"Disk: {disk_pct:.0f}%")
                
                if problems:
                    last = self._panel_alerts.get(key, 0)
                    if time.time() - last > self._panel_alert_cooldown:
                        self._panel_alerts[key] = time.time()
                        if self.admin_bot: 
                            msg = f"⚠️ Panel {key}:\n" + "\n".join(f"- {p}" for p in problems)
                            self.admin_bot.msg(msg)
                else:
                    self._panel_alerts.pop(key, None)
            except Exception:
                self.log.error("health check failed for panel %s (%s)", panel.name, panel.address, exc_info=True)

    def reconcile_inbounds(self) -> None:
        """Re-sync every user to every panel (idempotent).

        Creates missing panel clients and attaches any VLESS inbounds the
        existing clients lack (e.g. after an admin adds an inbound to a
        panel). One user's panel rejection must not abort the rest; the
        next cycle retries the failures.

        One client list per panel for the whole cycle, filled here — not one
        ``clients/get`` per user. Users stay serial: each ``add_users``
        already waits on the panel pool, and a user worker on that same pool
        would deadlock.
        """
        failures: list[str] = []
        known = self.sub.panel_svc.client_maps(self.sub.panels)
        for username in self.sub.user_svc.list_users():
            try:
                self.sub.business_svc.add_users(
                    username, _called_internally=True, known_clients=known,
                )
            except AppError:
                self.log.error("inbound reconcile failed for %s", username, exc_info=True)
                failures.append(username)
        if failures:
            shown = ", ".join(failures[:10])
            self._alert_admin(
                f"⚠️ Inbound reconcile failed for {len(failures)} user(s): {shown}"
            )

    ### Snapshots ###

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

    ### Calendar ###

    def is_first(self) -> None:
        # NOTE: This function is NOT meant to be called like `bwatch_instance.is_first()`.
        # NOTE: Exclusive to one thread only.
        now = datetime.now(timezone.utc)
        if now.day != 1:
            return
        today = now.strftime("%Y-%m-%d")

        # Restart-safe: compare against stored month key, not just the date string.
        # '_last_reset_month' stores "YYYY-MM" so a process restart on day 2
        # doesn't accidentally re-trigger a reset that already happened.
        current_month = now.strftime("%Y-%m")
        self.db.reset_monthly(current_month, today)
    
    def reset(self) -> None:
        self.db.clear_notifications()

    ### Scheduler ###

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
            ("notification reset", "reset"),
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
            self.log.error("%s crashed", what, exc_info=True)

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
