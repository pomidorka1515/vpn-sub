from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from db.database import Database
from errors import (
    PanelRejectedError,
)
from tests.support.panels import FakePanel, make_panel_client
from tests.support.subscriptions import close_subscription, make_subscription
from tests.support.users import USER_UUID

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


NEW_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.mark.asyncio
async def test_reset_finalization_and_startup_recovery(
    async_db: Database,
    tmp_path: Path,
) -> None:
    from errors import DatabaseError

    sub = await make_subscription(async_db, tmp_path)
    try:
        await create_alice(async_db)
        await async_db.set_auth_token("alice", "x" * 100)
        await async_db.set_metadata(
            "user_reset_pending:alice",
            '{"uuid":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","token":"new-token"}',
        )
        with pytest.raises(DatabaseError, match="reset UUID"):
            await async_db.finalize_reset("alice", NEW_UUID, "new-token")
        assert await async_db.get_metadata("user_reset_pending:alice") is not None
        await async_db.update_user("alice", uuid=NEW_UUID)
        await sub.start()
        user = await async_db.get_user("alice")
        assert user is not None
        assert user["token"] == "new-token"
        assert user["auth_token"] is None
        assert await async_db.get_metadata("user_reset_pending:alice") is None
    finally:
        await close_subscription(sub)


@pytest.mark.asyncio
@pytest.mark.parametrize("db_uuid", [USER_UUID, NEW_UUID])
async def test_reset_recovery_replays_partial_panel_updates(
    async_db: Database,
    tmp_path: Path,
    db_uuid: str,
) -> None:
    updated = FakePanel(name="updated", clients=[make_panel_client("alice", [1], uuid=NEW_UUID)])
    stale = FakePanel(name="stale", clients=[make_panel_client("alice", [1])])
    sub = await make_subscription(async_db, tmp_path, [updated, stale])
    try:
        await create_alice(async_db)
        await async_db.update_user("alice", uuid=db_uuid)
        await async_db.set_auth_token("alice", "x" * 100)
        await async_db.set_metadata(
            "user_reset_pending:alice",
            json.dumps({"uuid": NEW_UUID, "token": "new-token", "old_uuid": USER_UUID}),
        )
        await async_db.set_metadata("uuid_rollback_failed:alice", "interrupted")
        await sub.start()
        user = await async_db.get_user("alice")
        assert user is not None
        assert (user["uuid"], user["token"], user["auth_token"]) == (NEW_UUID, "new-token", None)
        assert updated.clients[0].uuid == stale.clients[0].uuid == NEW_UUID
        assert await async_db.get_metadata("user_reset_pending:alice") is None
        assert await async_db.get_metadata("uuid_rollback_failed:alice") is None
        await sub.business_svc.reconcile_user("alice")
    finally:
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_reset_recovery_retains_intent_until_panel_retry_succeeds(
    async_db: Database,
    tmp_path: Path,
) -> None:
    accepted = FakePanel(name="accepted", clients=[make_panel_client("alice", [1])])
    rejected = FakePanel(
        name="rejected",
        clients=[make_panel_client("alice", [1])],
        post_payload={"success": False, "msg": "rejected", "obj": None},
    )
    sub = await make_subscription(async_db, tmp_path, [accepted, rejected])
    try:
        await create_alice(async_db)
        await async_db.set_metadata(
            "user_reset_pending:alice",
            '{"uuid":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","token":"new-token"}',
        )
        await sub.start()
        user = await async_db.get_user("alice")
        assert user is not None
        assert (user["uuid"], user["token"]) == (USER_UUID, "a" * 40)
        assert accepted.clients[0].uuid == NEW_UUID
        assert rejected.clients[0].uuid == USER_UUID
        assert await async_db.get_metadata("user_reset_pending:alice") is not None
        assert await async_db.get_metadata("uuid_rollback_failed:alice") is not None
        with pytest.raises(PanelRejectedError):
            await sub.business_svc.reset_user("alice")
        assert await async_db.get_metadata("user_reset_pending:alice") is not None
        rejected._post_payload = None
        await sub.business_svc.reconcile_user("alice")
        user = await async_db.get_user("alice")
        assert user is not None
        assert (user["uuid"], user["token"]) == (NEW_UUID, "new-token")
        assert accepted.clients[0].uuid == rejected.clients[0].uuid == NEW_UUID
        assert await async_db.get_metadata("user_reset_pending:alice") is None
        assert await async_db.get_metadata("uuid_rollback_failed:alice") is None
    finally:
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_reset_request_recovers_previous_reset_before_starting_another(
    async_db: Database,
    tmp_path: Path,
) -> None:
    panel = FakePanel(clients=[make_panel_client("alice", [1])])
    sub = await make_subscription(async_db, tmp_path, [panel])
    try:
        await create_alice(async_db)
        await async_db.set_metadata(
            "user_reset_pending:alice",
            '{"uuid":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","token":"new-token"}',
        )
        result = await sub.business_svc.reset_user("alice")
        user = await async_db.get_user("alice")
        assert user is not None
        assert (user["uuid"], user["token"]) == (result.uuid, result.token)
        assert panel.clients[0].uuid == result.uuid
        assert len(panel.posts) == 2
        assert await async_db.get_metadata("user_reset_pending:alice") is None
    finally:
        await close_subscription(sub)


@pytest_asyncio.fixture
async def async_db(tmp_path: Path) -> AsyncGenerator[Database]:
    db = Database(path=tmp_path / "state.sqlite3", start_backup=False)
    await db.start()
    try:
        yield db
    finally:
        await db.close()


async def create_alice(db: Database) -> None:
    await db.create_user(
        username="alice",
        uuid=USER_UUID,
        token="a" * 40,
        fingerprint="chrome",
        displayname="Alice",
        bw_limit_gb=10,
        wl_limit_gb=5,
    )
