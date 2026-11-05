from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import pytest_asyncio
from async_api_helpers import Audit, api_app, config_snapshot
from async_api_helpers import make_subscription as make_api_subscription
from async_api_helpers import make_watch as make_api_watch
from helpers import SUBSCRIPTIONS, make_subscription, make_watch, subscription_config

from api import AsyncApi
from config import AppConfig, Config, LangConfig
from config.store import AsyncLinesConfig
from config.validation import check_config_document
from db import Database
from threadutil import named_to_thread

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from quart import Quart

    from bwatch import BWatch
    from core import Subscription

__all__ = ["admin_api", "database", "db_path", "language", "subscription", "watch"]


@pytest.fixture
def cfg(tmp_path: Path) -> Iterator[Config]:
    config = Config(path=tmp_path / "config.json", backup_dir=None, sync_mode="none")
    yield config
    config.close()


@pytest.fixture
def app_cfg(tmp_path: Path) -> Iterator[Config[AppConfig]]:
    document: AppConfig = {
        "uri": "sub",
        "api_token": "test-admin-token-long-enough",
        "provider_id": "test",
        "salt": "test",
        "domain": "example.test",
        "ping_check_url": "https://example.test/ping",
        "sub_name": "test",
        "api_admin_ui_auth": ["admin", "password"],
        "fingerprints": ["chrome"],
        "bypass_packages": [],
        "panel_alert_cooldown": 3600,
        "nodes": {},
        "profiles": {},
        "redis": {"url": "redis://localhost/0"},
        "json_template": {
            "dns": {},
            "routing": {"rules": [], "domainStrategy": "AsIs"},
            "inbounds": [],
            "outbounds": [
                {
                    "settings": {"vnext": [{"address": "", "users": [{"id": ""}]}]},
                    "streamSettings": {},
                }
            ],
            "remarks": "test",
        },
        "3xui": {
            "local": {
                "name": "local",
                "address": "localhost",
                "port": 2053,
                "uri": "panel",
                "token": "test-token-at-least-20-characters",
                "https": False,
                "whitelist": False,
                "inbounds_list": [1],
                "mode": "whitelist",
            },
        },
        "bot": {"token": "test", "whitelist": [1]},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    config = Config[AppConfig](
        path=path,
        schema_path=Path(__file__).resolve().parents[2] / "config.schema.json",
        validators=(check_config_document,),
        backup_dir=None,
        sync_mode="none",
    )
    try:
        yield config
    finally:
        config.close()


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
async def admin_api(
    database: Database,
    language: Config[LangConfig],
) -> AsyncIterator[tuple[Quart, Subscription]]:
    async for app in api_app():
        sub = await make_api_subscription(database, app=app, lang_cfg=language)
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
            bw=make_api_watch(database, sub),
            limiter=app.extensions["limiter"],
        )
        yield app, sub


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
