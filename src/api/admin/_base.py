from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from quart import Quart

    from api.common import Route, SnapshotStatus
    from app_runtime.rate_limit import AsyncRateLimiter
    from config import AppConfig
    from config.store import AsyncConfig, AsyncLinesConfig
    from core import Subscription
    from loggers import Logger


class AsyncAdminApiMixin:
    """Attributes every admin route group reads from the composed Api."""

    app: Quart
    ROUTES: ClassVar[tuple[Route, ...]]
    cfg: AsyncConfig[AppConfig]
    sub: Subscription
    bw: SnapshotStatus
    uri: str
    log: Logger
    token: str
    ui_session_key: bytes
    limiter: AsyncRateLimiter
    audit_cfg: AsyncLinesConfig

    async def _backup_config_after_commit(self) -> None:
        # The durable config edit has already succeeded. Backup failure must
        # neither report a failed mutation nor prevent its audit append.
        try:
            await self.cfg.backup_now()
        except Exception:
            self.log.exception("config backup failed after commit")
