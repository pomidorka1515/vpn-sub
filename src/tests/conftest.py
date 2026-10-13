from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from helpers import SUBSCRIPTIONS, make_subscription, make_watch

from db import Database

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from bwatch import BWatch
    from core import Subscription

__all__ = ["database", "db_path", "subscription", "watch"]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.sqlite3"


@pytest_asyncio.fixture
async def database(db_path: Path) -> AsyncIterator[Database]:
    db = Database(path=db_path, timeout=2, start_backup=False)
    await db.start()
    try:
        yield db
    finally:
        from async_core_helpers import close_async_subscription

        for sub in list(SUBSCRIPTIONS):
            if sub.res.db is db:
                await close_async_subscription(sub)
                SUBSCRIPTIONS.remove(sub)
        await db.close()


@pytest_asyncio.fixture
async def subscription(database: Database) -> AsyncIterator[Subscription]:
    sub = await make_subscription(database)
    try:
        yield sub
    finally:
        from async_core_helpers import close_async_subscription

        await close_async_subscription(sub)


@pytest_asyncio.fixture
async def watch(database: Database, subscription: Subscription) -> AsyncIterator[BWatch]:
    watcher = make_watch(database, subscription)
    try:
        yield watcher
    finally:
        await watcher.close()
