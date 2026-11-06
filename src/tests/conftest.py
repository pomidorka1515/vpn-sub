from __future__ import annotations

import asyncio
import json
import shutil
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from config import AppConfig, Config, LangConfig
from config.validation import check_config_document
from db import Database
from tests.support.paths import REPO_ROOT
from tests.support.subscriptions import subscription_scope
from threadutil import named_to_thread

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path


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
        schema_path=REPO_ROOT / "config.schema.json",
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
        path=REPO_ROOT / "lang.jsonc",
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


@pytest.fixture
def database_read_connections() -> int:
    return 2


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
async def database(
    db_path: Path,
    database_template: Path,
    database_read_connections: int,
) -> AsyncIterator[Database]:
    # Closing the template checkpoints its WAL. Each test gets a private file
    # and real startup on its own loop; migration tests using db_path stay fresh.
    await named_to_thread("test-database-copy", shutil.copyfile, database_template, db_path)
    db = Database(
        path=db_path,
        timeout=2,
        read_connections=database_read_connections,
        start_backup=False,
    )
    await db.start()
    try:
        async with subscription_scope():
            yield db
    finally:
        await db.close()
