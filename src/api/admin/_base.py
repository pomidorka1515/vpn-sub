from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quart import Quart

    from api.common import SnapshotStatus
    from app_runtime.rate_limit import AsyncRateLimiter
    from config import AppConfig
    from config.store import AsyncConfig, AsyncLinesConfig
    from core import Subscription
    from loggers import Logger


class AsyncAdminApiMixin:
    """Attributes every admin route group reads from the composed Api."""

    app: Quart
    cfg: AsyncConfig[AppConfig]
    sub: Subscription
    bw: SnapshotStatus
    uri: str
    log: Logger
    token: str
    limiter: AsyncRateLimiter
    audit_cfg: AsyncLinesConfig
