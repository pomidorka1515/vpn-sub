from __future__ import annotations

from typing import TYPE_CHECKING, Literal
import threading

from custom_types import BandwidthInfo
from db import Database
from loggers import Logger
from bots import AdminBot, PublicBot
from config import ConfigLike

if TYPE_CHECKING:
    from core import Subscription


class BWatchHost:
    """Attributes supplied by ``BWatch``. Mixins do not construct them."""

    log: Logger
    cfg: ConfigLike
    db: Database
    sub: Subscription
    bot: PublicBot | None
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
