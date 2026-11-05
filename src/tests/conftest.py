from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from helpers import SUBSCRIPTIONS, make_subscription, make_watch

from config import Config, LangConfig
from db import Database
from threadutil import named_to_thread

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from bwatch import BWatch
    from core import Subscription

__all__ = ["database", "db_path", "language", "subscription", "watch"]


@pytest.fixture(scope="session")
def language() -> Iterator[Config[LangConfig]]:
    """Parse bundled translations once; consumers receive detached views."""
    cfg = Config[LangConfig](
        path=Path(__file__).resolve().parents[2] / "lang.jsonc",
        read_only=True,
        read_only_jsonc=True,
    )
    try:
        yield cfg
    finally:
        cfg.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.sqlite3"


@pytest.fixture(scope="session")
def database_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the empty schema once; never share live connections or test data."""
    path = tmp_path_factory.mktemp("database-template") / "state.sqlite3"

    async def initialize() -> None:
        db = Database(path=path, start_backup=False)
        await db.start()
        await db.close()

    asyncio.run(initialize())
    return path


@pytest_asyncio.fixture
async def database(db_path: Path, database_template: Path) -> AsyncIterator[Database]:
    # Closing the template checkpoints its WAL. Each test gets a private file
    # and real startup on its own loop; migration tests using db_path stay fresh.
    await named_to_thread("test-database-copy", shutil.copyfile, database_template, db_path)
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
