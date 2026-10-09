"""BWatch composition root."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Literal
import threading
import time

from custom_types import BandwidthInfo
from db import Database
from loggers import Logger
from bots import AdminBot, PublicBot
from config import ConfigLike

from .calendar import CalendarMixin
from .panels import PanelsMixin
from .quota import QuotaMixin
from .scheduler import SchedulerMixin
from .snapshots import SnapshotsMixin

if TYPE_CHECKING:
    from core import Subscription

__all__ = ["BWatch"]


class BWatch(QuotaMixin, PanelsMixin, SnapshotsMixin, CalendarMixin, SchedulerMixin):
    """Background quota, expiry, panel health, and snapshot watcher."""

    def __init__(
        self,
        cfg: ConfigLike,
        db: Database,
        sub: Subscription,
        bot: PublicBot | None = None,
        admin_bot: AdminBot | None = None,
        verbose: bool = False
    ) -> None:
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg: ConfigLike = cfg
            self.db = db
            self._stop_event = threading.Event()
            self._mem_lock = threading.Lock()  # single lock for all mem/wl_mem access
            self.sub: Subscription = sub
            self.bot: PublicBot | None = bot
            self.admin_bot: AdminBot | None = admin_bot
            self.verbose: bool = verbose
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
