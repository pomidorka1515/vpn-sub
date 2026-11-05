from __future__ import annotations

from typing import TYPE_CHECKING

import pytest_asyncio

from tests.support.subscriptions import close_subscription, make_subscription
from tests.support.watch import make_watch

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from bwatch import BWatch
    from db import Database


@pytest_asyncio.fixture
async def watch(database: Database) -> AsyncIterator[BWatch]:
    sub = await make_subscription(database)
    try:
        watcher = make_watch(database, sub)
        try:
            yield watcher
        finally:
            await watcher.close()
    finally:
        await close_subscription(sub)
