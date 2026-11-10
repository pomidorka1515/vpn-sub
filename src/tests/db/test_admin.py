from __future__ import annotations

import sqlite3
import time
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from db import Database
from db.admin import AdminIdentity

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio


async def test_schema_7_upgrade_preserves_users_codes_and_root_session(
    database: Database, db_path: Path
) -> None:
    await database.create_user(
        username="legacy", uuid="uuid", token="token", fingerprint="chrome", displayname="Legacy"
    )
    await database.set_admin_ui_session("legacy-root-session")
    await database.close()
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE admin_sessions")
        conn.execute("DROP TABLE admin_identities")
        conn.execute("UPDATE schema_version SET version = 7")
    upgraded = Database(path=db_path, start_backup=False)
    await upgraded.start()
    try:
        assert await upgraded.token_to_user("token") == "legacy"
        assert await upgraded.admin_ui_session() == "legacy-root-session"
        assert await upgraded.admin_identities("accounts") == []
        assert await upgraded.admin_identities("tokens") == []
    finally:
        await upgraded.close()


async def test_stale_actor_cannot_commit_and_stale_login_cannot_issue_session(
    database: Database,
) -> None:
    actor = AdminIdentity("actor", "accounts", "actor", "hash", ("auth.*",), 1)
    target = AdminIdentity("target", "tokens", "target", "digest", ("user.read",), 1)
    assert await database.admin_save_identity(actor, actor=None, create=True)
    assert await database.admin_save_identity(replace(actor, permissions=()), actor=None)
    assert not await database.admin_save_identity(target, actor=actor, create=True)
    assert not await database.admin_start_session(actor, "cookie", "bearer")
    assert await database.admin_identity("target") is None


async def test_sessions_expire_revoke_and_cascade(database: Database) -> None:
    account = AdminIdentity("account", "accounts", "operator", "hash", ("user.read",), 1)
    assert await database.admin_save_identity(account, actor=None, create=True)
    assert await database.admin_start_session(account, "cookie", "bearer")
    assert await database.admin_cookie_identity("cookie") == account
    assert await database.admin_bearer_identity("bearer") == account
    async with database.transaction() as conn:
        await conn.execute("UPDATE admin_sessions SET expires_at = ?", (int(time.time()) - 1,))
    assert await database.admin_cookie_identity("cookie") is None
    assert await database.admin_bearer_identity("bearer") is None
    assert await database.admin_start_session(account, "cookie-new", "bearer-new")
    assert await database.admin_delete_identity(account, actor=None)
    assert await database.admin_bearer_identity("bearer-new") is None
    async with database.connection() as conn:
        assert await conn.rows("SELECT * FROM admin_sessions") == []
