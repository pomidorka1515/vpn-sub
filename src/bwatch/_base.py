from __future__ import annotations

from typing import TYPE_CHECKING

from custom_types import BandwidthInfo

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from config import AppConfig
    from core import Subscription
    from core.common import Snapshot
    from db import Database
    from loggers import Logger
    from notify.notifier import Notifier

type CounterKey = tuple[str, str, str]  # panel, username, account UUID
type Counters = dict[CounterKey, BandwidthInfo]


class WatchJobs:
    """Shared state and hooks supplied by the composed watcher."""

    cfg: Snapshot[AppConfig]
    db: Database
    sub: Subscription
    log: Logger
    notifier: Notifier | None
    mem: Counters
    wl_mem: Counters
    _reset_mem: Counters
    _reset_wl_mem: Counters
    _usage_month: str | None
    snap_mem: Counters
    snap_wl_mem: Counters
    _snap_reset_mem: Counters
    _snap_reset_wl_mem: Counters
    _failure: dict[str, int] | None
    _panel_alerts: dict[str, float]

    async def _job(self, name: str, operation: Callable[[], Awaitable[None]]) -> None:
        raise NotImplementedError

    async def _alert_admin(self, message: str) -> None:
        raise NotImplementedError

    async def _quota(self, username: str, *, recover: bool) -> None:
        raise NotImplementedError
