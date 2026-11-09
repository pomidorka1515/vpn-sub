from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Unpack, cast

from api.application import create_api_application
from app_runtime.rate_limit import AsyncRateLimiter
from config import AppConfig
from config.store import AsyncConfig
from tests.support.config import config_mock
from tests.support.subscriptions import close_subscription
from tests.support.subscriptions import make_subscription as make_core_subscription

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from quart import Quart

    from app_runtime.rate_limit import RateLimitScript
    from config import Config, LangConfig
    from core import Subscription
    from db import Database
    from tests.support.contracts import AppOverrides
    from tests.support.panels import FakePanel


class Audit:
    async def read_all(self) -> list[dict[str, str]]:
        return []

    async def tail(self, n: int) -> list[dict[str, str]]:
        return []


class SnapshotStatus:
    """Snapshot status stub for API tests."""

    def get_daily_snapshot_failure(self) -> dict[str, str | int] | None:
        return None


class OpenRedis:
    def register_script(self, script: str) -> RateLimitScript:
        async def run(*, keys: tuple[str, ...], args: tuple[float, float, str, int, int]) -> int:
            if len(keys) == 2:
                return 0 if args[2] == "valid" else -1
            return 1

        return run

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def config_snapshot(document: AppConfig) -> AsyncConfig[AppConfig]:
    return cast(AsyncConfig[AppConfig], cast(object, config_mock(document)))


async def make_subscription(
    database: Database,
    *,
    app: Quart,
    lang_cfg: Config[LangConfig] | None = None,
    panels: list[FakePanel] | None = None,
    **overrides: Unpack[AppOverrides],
) -> Subscription:
    sub = await make_core_subscription(database, panels=panels, lang_cfg=lang_cfg, **overrides)
    app.extensions.setdefault("subscriptions", []).append(sub)
    return sub


def make_watch(database: Database, sub: Subscription) -> SnapshotStatus:
    return SnapshotStatus()


@asynccontextmanager
async def api_app() -> AsyncGenerator[Quart]:
    app = create_api_application(proxy_hops=0)
    limiter = AsyncRateLimiter("redis://test", client=OpenRedis())
    await limiter.start()
    app.extensions["limiter"] = limiter
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(limiter.close)
        try:
            yield app
        finally:
            for sub in app.extensions.get("subscriptions", []):
                cleanup.push_async_callback(close_subscription, sub)
