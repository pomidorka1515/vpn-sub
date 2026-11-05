from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest_asyncio

from api import AsyncApi
from config.store import AsyncLinesConfig
from tests.support.api import Audit, api_app, config_snapshot, make_subscription, make_watch
from tests.support.config import subscription_config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from quart import Quart

    from config import Config, LangConfig
    from core import Subscription
    from db import Database


@pytest_asyncio.fixture
async def admin_api(
    database: Database,
    language: Config[LangConfig],
) -> AsyncIterator[tuple[Quart, Subscription]]:
    async with api_app() as app:
        sub = await make_subscription(database, app=app, lang_cfg=language)
        AsyncApi(
            app=app,
            cfg=config_snapshot(
                subscription_config(
                    api_uri="api",
                    api_token="secret",
                    api_admin_ui_auth=["admin", "panel-secret"],
                ),
            ),
            audit_cfg=cast(AsyncLinesConfig, Audit()),
            sub=sub,
            bw=make_watch(database, sub),
            limiter=app.extensions["limiter"],
        )
        yield app, sub
