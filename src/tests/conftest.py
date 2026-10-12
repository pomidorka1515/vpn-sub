from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from helpers import make_subscription, make_watch

from db import AsyncDatabase, Database

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from bwatch import BWatch
    from core import Subscription

__all__ = ["database", "db_path", "subscription", "watch"]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.sqlite3"


@pytest.fixture
def database(db_path: Path) -> Iterator[Database]:
    db = Database(path=db_path, timeout=2)
    yield db
    db.close()


@pytest_asyncio.fixture
async def async_database(db_path: Path) -> AsyncIterator[AsyncDatabase]:
    db = AsyncDatabase(path=db_path, timeout=2, start_backup=False)
    await db.start()
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
def subscription(database: Database) -> Subscription:
    return make_subscription(database)


@pytest.fixture
def watch(database: Database, subscription: Subscription) -> BWatch:
    return make_watch(database, subscription)
