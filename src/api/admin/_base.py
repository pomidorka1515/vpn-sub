from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quart import Quart

    from api.common import SnapshotStatus
    from config import AppConfig
    from config.async_store import AsyncConfig, AsyncLinesConfig
    from core import AsyncSubscription
    from loggers import Logger


class AsyncAdminApiMixin:
    """Attributes every admin route group reads from the composed Api."""

    app: Quart
    cfg: AsyncConfig[AppConfig]
    sub: AsyncSubscription
    bw: SnapshotStatus
    uri: str
    log: Logger
    token: str
    audit_cfg: AsyncLinesConfig
