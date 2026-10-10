from __future__ import annotations

import threading
from concurrent.futures import Executor
from typing import TYPE_CHECKING, Literal

from bots import AdminBot, PublicBot
from config import AppConfig, Config, JsonValue
from custom_types import BandwidthInfo
from db import Database
from loggers import Logger
from tracer import TraceOp, trace

if TYPE_CHECKING:
    from core import Subscription


class BWatchHost:
    """Attributes supplied by ``BWatch``. Mixins do not construct them."""

    log: Logger
    cfg: Config[AppConfig]
    db: Database
    sub: Subscription
    bot: PublicBot | None
    verbose: bool
    admin_bot: AdminBot | None
    _stop_event: threading.Event
    _mem_lock: threading.Lock
    mem: dict[str, BandwidthInfo]
    wl_mem: dict[str, BandwidthInfo]
    snap_mem: dict[str, BandwidthInfo]
    snap_wl_mem: dict[str, BandwidthInfo]
    _snapshot_initialized: bool
    _panel_alerts: dict[str, int | float]
    _panel_alert_cooldown: int
    _snapshot_failures: dict[Literal["bandwidth", "state"], int]
    _snapshot_due_at: dict[Literal["bandwidth", "state"], float]
    _threads: tuple[threading.Thread, ...]

    def trace(self, operation: TraceOp, event: str, **fields: JsonValue) -> None:
        """
        Log a detailed operation, only when ``self.verbose`` is True.
        """
        if not self.verbose:
            return
        trace(self.log, operation, event, **fields)

    # Implemented by SchedulerMixin / SnapshotsMixin. Declared here so sibling
    # mixins can call them; pyright does not see methods across mixin classes.
    @staticmethod
    def _panel_pool() -> Executor:
        raise NotImplementedError

    def _alert_admin(self, message: str) -> None:
        raise NotImplementedError

    def record_daily_snapshot(self) -> None:
        raise NotImplementedError

    def record_snap_snapshot(self) -> None:
        raise NotImplementedError
