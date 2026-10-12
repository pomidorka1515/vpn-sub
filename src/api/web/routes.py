from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from api.common import AsyncBaseApi
from loggers import Logger

from .account import AsyncAccountRoutes
from .session import AsyncSessionRoutes
from .static import AsyncStaticRoutes

if TYPE_CHECKING:
    from quart import Quart

    from api.common import Route, SnapshotStatus
    from api.decorators.rate_limit import AsyncRateLimiter
    from config import AppConfig
    from config.async_store import AsyncConfig
    from core import AsyncSubscription

__all__ = ["AsyncWebApi"]


class AsyncWebApi(
    AsyncAccountRoutes,
    AsyncSessionRoutes,
    AsyncStaticRoutes,
    AsyncBaseApi,
):
    """Public, user-facing API."""

    ROUTES: ClassVar[tuple[Route, ...]] = (
        *AsyncStaticRoutes.ROUTES,
        *AsyncSessionRoutes.ROUTES,
        *AsyncAccountRoutes.ROUTES,
    )

    def __init__(self, app: Quart, cfg: AsyncConfig[AppConfig], sub: AsyncSubscription, bw: SnapshotStatus, limiter: AsyncRateLimiter) -> None:
        self.log = Logger(type(self).__name__)
        conf = cfg.view()
        uri = "/" + "/".join(p for p in conf["uri"].split("/") if p)
        super().__init__(app, cfg, sub, bw, uri, limiter)
        self.prefix = self.uri.rstrip("/")
