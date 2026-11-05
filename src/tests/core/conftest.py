from __future__ import annotations

from typing import TYPE_CHECKING

import pytest_asyncio

from tests.support.subscriptions import close_subscription, make_subscription

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from core import Subscription
    from db import Database


@pytest_asyncio.fixture
async def subscription(database: Database) -> AsyncIterator[Subscription]:
    sub = await make_subscription(database)
    try:
        yield sub
    finally:
        await close_subscription(sub)
