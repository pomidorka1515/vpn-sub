from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from api.common import AsyncBaseApi
from loggers import Logger

from .code import AsyncCodeRoutes
from .config import AsyncConfigRoutes
from .logs import AsyncLogsRoutes
from .operations import AsyncOperationsRoutes
from .panel import AsyncPanelRoutes
from .state import AsyncStateRoutes
from .ui import AsyncUiRoutes
from .user import AsyncUserRoutes

if TYPE_CHECKING:
    from quart import Quart

    from api.common import Route, SnapshotStatus
    from api.decorators.rate_limit import AsyncRateLimiter
    from config import AppConfig
    from config.store import AsyncConfig, AsyncLinesConfig
    from core import Subscription

__all__ = ["AsyncApi"]


class AsyncApi(
    AsyncUserRoutes,
    AsyncPanelRoutes,
    AsyncCodeRoutes,
    AsyncStateRoutes,
    AsyncLogsRoutes,
    AsyncOperationsRoutes,
    AsyncConfigRoutes,
    AsyncUiRoutes,
    AsyncBaseApi,
):
    """Private admin API."""

    ROUTES: ClassVar[tuple[Route, ...]] = (
        *AsyncUserRoutes.ROUTES,
        *AsyncPanelRoutes.ROUTES,
        *AsyncCodeRoutes.ROUTES,
        *AsyncStateRoutes.ROUTES,
        *AsyncLogsRoutes.ROUTES,
        *AsyncOperationsRoutes.ROUTES,
        *AsyncConfigRoutes.ROUTES,
    )

    def __init__(
        self,
        app: Quart,
        cfg: AsyncConfig[AppConfig],
        audit_cfg: AsyncLinesConfig,
        sub: Subscription,
        bw: SnapshotStatus,
        limiter: AsyncRateLimiter,
    ) -> None:
        self.log = Logger(type(self).__name__)
        conf = cfg.view()
        uri = "/" + "/".join(
            p.strip("/ ") for p in (conf["uri"], conf.get("api_uri", "")) if p and p.strip("/ ")
        )
        self.token = conf["api_token"]
        self.audit_cfg = audit_cfg
        super().__init__(app, cfg, sub, bw, uri, limiter)
