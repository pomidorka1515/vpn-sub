from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from bwatch import BWatch
from core import Subscription
from db import Database
from helpers import make_subscription, make_watch

__all__ = ["db_path", "database", "subscription", "watch"]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.sqlite3"


@pytest.fixture
def database(db_path: Path) -> Iterator[Database]:
    db = Database(path=db_path, timeout=2)
    yield db
    db.close()


@pytest.fixture
def subscription(database: Database) -> Subscription:
    return make_subscription(database)


@pytest.fixture
def watch(database: Database, subscription: Subscription) -> BWatch:
    return make_watch(database, subscription)
