from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Unpack, cast

from async_core_helpers import close_async_subscription, make_async_subscription
from helpers import config_mock, subscription_config

from api.application import create_api_application
from app_runtime.rate_limit import AsyncRateLimiter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from helpers import FakePanel
    from quart import Quart
    from typing_contracts import AppOverrides

    from app_runtime.rate_limit import RateLimitScript
    from config import AppConfig, Config, LangConfig
    from config.store import AsyncConfig
    from core import Subscription
    from db import Database


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
    return cast("AsyncConfig[AppConfig]", cast("object", config_mock(document)))


async def make_subscription(
    database: Database,
    *,
    app: Quart,
    lang_cfg: Config[LangConfig] | None = None,
    panels: list[FakePanel] | None = None,
    **overrides: Unpack[AppOverrides],
) -> Subscription:
    sub = await make_async_subscription(database, Path(database.path).parent, panels=panels)
    sub.res.cfg.view.side_effect = lambda: subscription_config(**overrides)  # type: ignore[attr-defined]
    if lang_cfg is not None:
        language = await sub.res.offloads.files.run(lang_cfg.view)
        sub.res.lang_cfg.view.side_effect = lambda: language  # type: ignore[attr-defined]
    app.extensions.setdefault("subscriptions", []).append(sub)
    return sub


def make_watch(database: Database, sub: Subscription) -> SnapshotStatus:
    return SnapshotStatus()


async def api_app() -> AsyncIterator[Quart]:
    app = create_api_application(proxy_hops=0)
    limiter = AsyncRateLimiter("redis://test", client=OpenRedis())
    await limiter.start()
    app.extensions["limiter"] = limiter
    yield app
    for sub in app.extensions.get("subscriptions", []):
        await close_async_subscription(sub)
    await limiter.close()
