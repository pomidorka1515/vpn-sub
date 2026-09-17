from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Literal
import threading
import time

from custom_types import BandwidthInfo, BandwidthUpdate
from db import Database
from errors import AppError, PanelUnavailableError
from loggers import Logger
from protocols import AdminBotLike, PublicBotLike
from config import ConfigLike
from util import SysUtil

if TYPE_CHECKING:
    from core import Subscription

__all__ = ["BWatch"]


class BWatch:
    def _alert_admin(self, message: str) -> None:
        if self.admin_bot:
            self.admin_bot.msg(message)

    def __init__(
        self, 
        cfg: ConfigLike, 
        db: Database,
        sub: Subscription, 
        bot: PublicBotLike | None = None,
        admin_bot: AdminBotLike | None = None
    ):
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg: ConfigLike = cfg
            self.db = db
            self._stop_event = threading.Event()
            self._mem_lock = threading.Lock()  # single lock for all mem/wl_mem access
            self.sub: Subscription = sub
            self.bot: PublicBotLike | None = bot
            self.admin_bot: AdminBotLike | None = admin_bot
            self.mem: dict[str, BandwidthInfo] = {}
            self.wl_mem: dict[str, BandwidthInfo] = {}
            self._snapshot_initialized: bool = False
            self._panel_alerts: dict[str, int | float] = {} # only used by 1 thread, no lock needed yet
            self._panel_alert_cooldown: int = self.cfg.get('panel_alert_cooldown', as_type=int) or 3600
            self._snapshot_failures: dict[Literal["bandwidth", "state"], int] = {}
            self._snapshot_due_at: dict[Literal["bandwidth", "state"], float] = {
                "bandwidth": 0.0,
                "state": 0.0,
            }

            _threads: tuple[tuple[Callable[..., object], str], ...] = (
                (self._every_120s, "Quota & Notifs"),
                (self._every_2h, "Date check"),
                (self._every_15s, "Bandwidth"),
                (self._every_24h, "Snapshots"),
                (self._every_5m, "Panels check"),
                (self._every_24h_snapshot, "Daily snapshots"),
            )
            self._threads: tuple[threading.Thread, ...] = tuple(
                threading.Thread(target=target, name=name, daemon=True)
                for target, name in _threads
            )

    def start(self) -> None:
        initial_mem: dict[str, BandwidthInfo] = {}
        initial_wl_mem: dict[str, BandwidthInfo] = {}
        for i in self.sub.list_users():
            initial_wl_mem[i] = self.sub.bandwidth(username=i, whitelist=True)
            if self.sub.get_user_state(i)['bw_limit_gb'] == 0:
                continue
            initial_mem[i] = self.sub.bandwidth(username=i)

        with self._mem_lock:
            self.mem = initial_mem
            self.wl_mem = initial_wl_mem
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
        
    def _update_user(self, *args: Any, **kwargs: Any) -> None:
        try:
            self.sub.update_user(*args, **kwargs)
        except AppError:
            self.log.error("background user update failed", exc_info=True)
    
    # prune_old_<cfg name>_snapshots

    def prune_old_bw_snapshots(self) -> None:
        retention = int(self.db.get_metadata("bw_retention_days", "30") or 30)
        cutoff = int(time.time()) - retention * 86400
        self.db.prune_bandwidth_snapshots(cutoff)
    
    def prune_old_snap_snapshots(self) -> None:
        retention = int(self.db.get_metadata("state_retention_days", "30") or 30)
        cutoff = int(time.time()) - retention * 86400
        self.db.prune_state_snapshots(cutoff)
    
    def bandwidth_check(self) -> None:
        updates: dict[str, BandwidthUpdate] = {}    # username -> (delta, current) for main
        wl_updates: dict[str, BandwidthUpdate] = {} # username -> (delta, current) for whitelist
        for i in self.sub.list_users():
            state = self.sub.get_user_state(i)
            # Main bandwidth
            if int(state['expires_at']) != 0:
                if (int(state['expires_at']) - int(time.time())) >= 0 and not bool(state['enabled_time']):
                    self._update_user(username=i, enable=True, timee=True)

            main_required = int(state['bw_limit_gb']) != 0
            wl_required = int(state['wl_limit_gb']) != 0
            if main_required and int(state['bw_used']) < int(int(state['bw_limit_gb']) * 10**9) and not bool(state['enabled']):
                self._update_user(username=i, enable=True)
            if wl_required and int(state['wl_used']) < int(int(state['wl_limit_gb']) * 10**9) and not bool(state['enabled_wl']):
                self._update_user(username=i, wl_enable=True)

            # Read both counters before advancing either baseline. If either required
            # read fails, do not commit a partial delta for this user.
            try:
                current_bws = self.sub.bandwidth(username=i) if main_required else None
                current_wl_bws = self.sub.bandwidth(username=i, whitelist=True) if wl_required else None
            except Exception:
                self.log.error("bandwidth poll failed for user %s", i, exc_info=True)
                continue

            with self._mem_lock:
                if main_required:
                    assert current_bws is not None
                    if i not in self.mem:
                        self.mem[i] = current_bws
                    else:
                        delta = int(current_bws.total - self.mem[i].total)
                        if delta > 0:
                            updates[i] = BandwidthUpdate(delta=delta, current=current_bws)
                if wl_required:
                    assert current_wl_bws is not None
                    if i not in self.wl_mem:
                        self.wl_mem[i] = current_wl_bws
                    else:
                        delta = int(current_wl_bws.total - self.wl_mem[i].total)
                        if delta > 0:
                            wl_updates[i] = BandwidthUpdate(delta=delta, current=current_wl_bws)

        if not updates and not wl_updates:
            return

        with self._mem_lock:
            for i, update in updates.items():
                self.db.increment_usage(i, regular=update.delta)
                self.mem[i] = update.current
            for i, update in wl_updates.items():
                self.db.increment_usage(i, whitelist=update.delta)
                self.wl_mem[i] = update.current

    def panel_health_check(self) -> None:
        """Check each panel's Xray status and resource usage. Alert on issues."""
        for panel in self.sub.panels:
            if panel.dead:
                continue
            try:
                status = self.sub.getstatus(panel)
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

    def check(self) -> None:
        for i in self.sub.list_users():
            state = self.sub.get_user_state(i)
            tg_user = self.sub.get_username_telegram(tgid=i, reverse=True)
            expires_at = int(state['expires_at'])
            bw_limit = int(state['bw_limit_gb'])
            bw_used = int(state['bw_used'])
            wl_limit = int(state['wl_limit_gb'])
            wl_used = int(state['wl_used'])
            if expires_at != 0:
                if (expires_at - int(time.time())) <= 0:
                    if bool(state['enabled_time']):
                        self._update_user(username=i, enable=False, timee=False)
                        if self.bot: self.bot.msg(tg_user, 'warning_disabled') # sub expired
                    continue
                else:
                    days = (expires_at - int(time.time())) // 86400
                    if days <= 2 and tg_user is not None and self.db.mark_notification("regular", tg_user):
                            if self.bot: self.bot.msg(tg_user, 'warning_days', days=days)
            if wl_limit != 0 and wl_used > int(wl_limit * 10**9):
                if bool(state['enabled_wl']):
                    self._update_user(username=i, wl_enable=False)
                    if self.bot: self.bot.msg(tg_user, 'warning_traffic_whitelist_disabled', available=wl_limit)
            elif wl_limit != 0 and wl_used > int(wl_limit * 10**9 * 0.95) and tg_user is not None and self.db.mark_notification("whitelist", tg_user):
                    if self.bot: self.bot.msg(tg_user, 'warning_traffic_whitelist', used=int(round(wl_used / 10**6, 0)), available=wl_limit)
            if not bool(state['enabled']):
                continue
            if bw_limit == 0:
                continue
            if bw_used > int(bw_limit * 10**9):
                self._update_user(username=i, enable=False, timee=True)
                if self.bot: self.bot.msg(tg_user, 'warning_traffic_disabled', available=bw_limit)
            elif bw_used > int(bw_limit * 10**9 * 0.95) and tg_user is not None and self.db.mark_notification("regular", tg_user):
                    if self.bot: self.bot.msg(tg_user, 'warning_traffic', used=int(round(bw_used / 10**6, 0)), available=bw_limit)

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

    def record_snap_snapshot(self) -> None:
        """Record one state snapshot (`SysUtil` + panels) for today."""
        panels_data: dict[str, object] = {}
        panel_errors: list[str] = []
        for panel in self.sub.panels:
            status = self.sub.getstatus(panel)
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

    def record_daily_snapshot(self) -> None:
        """Record one bandwidth snapshot per user for today (UTC midnight).

        Thread-safe: takes a copy of mem under lock, releases it for I/O,
        then updates mem + writes config under lock."""

        # 1. Snapshot mem under lock
        with self._mem_lock:
            mem_snapshot = dict(self.mem)
            wl_mem_snapshot = dict(self.wl_mem)

        # 2. Fetch bandwidth outside lock (I/O can be slow)
        midnight = int(time.time()) - (int(time.time()) % 86400)
        # {username: (current, wl_current, delta_up, delta_down, wl_up, wl_down)}
        snapshot_data: dict[str, tuple[BandwidthInfo, BandwidthInfo, int, int, int, int]] = {}
        eligible_users: list[str] = []
        failed_users: list[str] = []

        for username in self.sub.list_users():
            state = self.sub.get_user_state(username)
            bw_limit = int(state['bw_limit_gb'])
            wl_limit = int(state['wl_limit_gb'])
            if bw_limit == 0 and wl_limit == 0:
                continue
            eligible_users.append(username)

            try:
                current = self.sub.bandwidth(username=username)
                wl_current = self.sub.bandwidth(username=username, whitelist=True)
            except Exception:
                failed_users.append(username)
                self.log.error(
                    "failed to record daily bandwidth snapshot for user %s",
                    username,
                    exc_info=True,
                )
                continue
            
            last_mem = mem_snapshot.get(username)
            last_wl_mem = wl_mem_snapshot.get(username)

            snapshot_data[username] = (
                current, wl_current,
                max(int(current.upload - last_mem.upload), 0) if last_mem else 0,
                max(int(current.download - last_mem.download), 0) if last_mem else 0,
                max(int(wl_current.upload - last_wl_mem.upload), 0) if last_wl_mem else 0,
                max(int(wl_current.download - last_wl_mem.download), 0) if last_wl_mem else 0,
            )

        # 3. Update mem + write snapshot under lock (atomic)
        with self._mem_lock:
            for username, (current, wl_current, up, down, wl_up, wl_down) in snapshot_data.items():
                self.mem[username] = current
                self.wl_mem[username] = wl_current

                self.db.add_bandwidth_snapshot(username, midnight, up, down, wl_up, wl_down)

        self.prune_old_bw_snapshots()

        if eligible_users and failed_users:
            self.db.set_metadata(
                "daily_bw_snapshot_failures",
                f"{int(time.time())}:{len(failed_users)}:{len(eligible_users)}",
            )
        else:
            self.db.delete_metadata("daily_bw_snapshot_failures")
        if eligible_users and len(failed_users) == len(eligible_users):
            raise PanelUnavailableError(
                f"Daily bandwidth snapshot failed for all {len(eligible_users)} eligible user(s)"
            )

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

    ### Helper functions ###
    def _every_120s(self) -> None:
        while not self._stop_event.wait(120):
            self.check()
    def _every_2h(self) -> None:
        while not self._stop_event.wait(7200):
            self.is_first()
    def _every_15s(self) -> None:
        while not self._stop_event.wait(15):
            self.bandwidth_check()
    def _every_24h(self) -> None:
        while not self._stop_event.wait(86400):
            self.reset()
            self.prune_old_bw_snapshots()
            self.prune_old_snap_snapshots()
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

    def _every_24h_snapshot(self) -> None:
        while True:
            now = time.monotonic()
            self._run_due_daily_snapshots(now)
            due_snapshots = tuple(
                kind for kind, due_at in self._snapshot_due_at.items() if due_at <= now
            )
            if not due_snapshots:
                next_delay = min(self._snapshot_due_at.values()) - now
                if self._stop_event.wait(next_delay):
                    return
                continue

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

    def _every_5m(self) -> None:
        while not self._stop_event.wait(300):
            self.panel_health_check()
