from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from db import Database
from errors import DatabaseError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncGenerator[Database]:
    database = Database(path=tmp_path / "episodes.sqlite3", start_backup=False)
    await database.start()
    try:
        yield database
    finally:
        await database.close()


async def create_user(db: Database) -> None:
    await db.create_user(
        username="alice",
        uuid="original",
        token="token",
        fingerprint="chrome",
        displayname="Alice",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("registration", [False, True])
async def test_creation_rollbacks_clear_only_deleted_identity(
    db: Database, registration: bool
) -> None:
    if registration:
        await db.add_code("invite", "register")
        await db.register_with_code(
            code="invite",
            username="alice",
            uuid="original",
            token="token",
            fingerprint="chrome",
            displayname="Alice",
            ext_username="alice",
            ext_password_hash="hash",
        )
    else:
        await create_user(db)

    markers = {"alice:traffic_soon:80", "alice:restored", "alice2:traffic_soon:80"}
    intent = json.dumps({"uuid": "original", "notices": [{"kind": "RESTORED"}]})
    await db.sync_notifications(set(), markers)
    await db.set_metadata("user_notifications_pending:alice", intent)

    if registration:
        await db.rollback_registration_sync("alice", ("stale", "invite", False))
    else:
        with pytest.raises(DatabaseError, match="identity changed"):
            await db.rollback_created_user("alice", "stale")
    assert await db.notification_markers() == markers
    assert await db.get_metadata("user_notifications_pending:alice") == intent

    if registration:
        await db.rollback_registration_sync("alice", ("original", "invite", False))
    else:
        await db.rollback_created_user("alice", "original")
    assert not await db.user_exists("alice")
    assert await db.notification_markers() == {"alice2:traffic_soon:80"}
    assert await db.get_metadata("user_notifications_pending:alice") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("regular", "whitelist", "cleared"),
    [(-5, 0, "traffic"), (0, -5, "whitelist")],
)
async def test_usage_decrease_clears_only_changed_side(
    db: Database, regular: int, whitelist: int, cleared: str
) -> None:
    await create_user(db)
    await db.increment_usage("alice", regular=10, whitelist=10)
    markers = {
        "alice:traffic_soon:80",
        "alice:whitelist_soon:80",
        "alice:expiry_soon:1",
        "alice:traffic_exhausted:1",
        "alice:whitelist_exhausted:1",
        "alice:restored",
    }
    await db.sync_notifications(set(), markers)
    notices = [
        {"kind": kind}
        for kind in (
            "TRAFFIC_SOON",
            "TRAFFIC_EXHAUSTED",
            "WHITELIST_SOON",
            "WHITELIST_EXHAUSTED",
            "EXPIRY_SOON",
            "RESTORED",
        )
    ]
    await db.set_metadata(
        "user_notifications_pending:alice", json.dumps({"uuid": "original", "notices": notices})
    )

    await db.increment_usage("alice", regular=regular, whitelist=whitelist)
    assert await db.notification_markers() == markers - {f"alice:{cleared}_soon:80"}
    pending = await db.get_metadata("user_notifications_pending:alice")
    assert pending is not None
    saved = json.loads(pending)
    assert saved["uuid"] == "original"
    assert saved["notices"] == [
        item for item in notices if not item["kind"].startswith(cleared.upper())
    ]


@pytest.mark.asyncio
async def test_positive_and_missing_user_usage_do_not_clear_markers(db: Database) -> None:
    await create_user(db)
    markers = {"alice:traffic_soon:80", "missing:traffic_soon:80"}
    await db.sync_notifications(set(), markers)
    await db.increment_usages({"alice": (5, 0), "missing": (-5, 0)})
    assert await db.notification_markers() == markers


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["traffic", "whitelist", "expiry"])
async def test_translated_user_fields_clear_only_affected_threshold(
    db: Database, side: str
) -> None:
    await create_user(db)
    markers = {
        "alice:traffic_soon:80",
        "alice:whitelist_soon:80",
        "alice:expiry_soon:1",
        "alice:traffic_exhausted:1",
        "alice:restored",
    }
    await db.sync_notifications(set(), markers)
    if side == "traffic":
        await db.set_user("alice", bw_limit=1, bw_used=1)
    elif side == "whitelist":
        await db.set_user("alice", wl_bw_limit=1, wl_bw_used=1)
    else:
        await db.set_user("alice", expiry_time=1)
    assert await db.notification_markers() == {
        marker for marker in markers if not marker.startswith(f"alice:{side}_soon:")
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved", [False, True])
async def test_uuid_rotation_retains_recovery_intent_with_new_identity(
    db: Database, reserved: bool
) -> None:
    await create_user(db)
    markers = {
        "alice:traffic_soon:80",
        "alice:whitelist_soon:80",
        "alice:expiry_soon:1",
        "alice:traffic_exhausted:1",
        "alice:restored",
    }
    await db.sync_notifications(set(), markers)
    notices = [{"kind": "TRAFFIC_SOON"}, {"kind": "RESTORED"}]
    await db.set_metadata(
        "user_notifications_pending:alice", json.dumps({"uuid": "original", "notices": notices})
    )
    if reserved:
        await db.reserve_uuid("alice", "original", "replacement", "marker")
        await db.finalize_uuid("alice", "original", "replacement")
    else:
        await db.set_user("alice", userid="replacement")
    assert await db.notification_markers() == {"alice:traffic_exhausted:1", "alice:restored"}
    pending = await db.get_metadata("user_notifications_pending:alice")
    assert pending is not None
    assert json.loads(pending) == {"uuid": "replacement", "notices": [{"kind": "RESTORED"}]}


@pytest.mark.asyncio
async def test_bonus_preserves_unlimited_and_unchanged_sides(db: Database) -> None:
    await create_user(db)
    await db.set_user("alice", bw_limit=10)
    markers = {
        "alice:traffic_soon:80",
        "alice:whitelist_soon:80",
        "alice:expiry_soon:1",
        "alice:traffic_exhausted:1",
    }
    await db.sync_notifications(set(), markers)
    await db.add_code("bonus", "bonus", gb=5, wl_gb=5, days=1)
    await db.adjust_bonus("alice", "bonus")
    assert await db.notification_markers() == markers - {"alice:traffic_soon:80"}
    user = await db.get_user("alice")
    assert user is not None
    assert (user["bw_limit_gb"], user["wl_limit_gb"], user["expires_at"]) == (15, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["creation", "registration", "usage"])
async def test_episode_cleanup_failure_rolls_back_mutation(db: Database, mutation: str) -> None:
    if mutation == "registration":
        await db.add_code("invite", "register")
        await db.register_with_code(
            code="invite",
            username="alice",
            uuid="original",
            token="token",
            fingerprint="chrome",
            displayname="Alice",
            ext_username="alice",
            ext_password_hash="hash",
        )
    else:
        await create_user(db)
    await db.increment_usage("alice", regular=10)
    marker = "alice:traffic_soon:80"
    await db.sync_notifications(set(), {marker})
    async with db.transaction(immediate=True) as conn:
        await conn.execute(
            "CREATE TRIGGER reject_episode_delete BEFORE DELETE ON notification_state "
            "BEGIN SELECT RAISE(ABORT, 'cleanup failed'); END"
        )

    async def mutate() -> None:
        if mutation == "creation":
            await db.rollback_created_user("alice", "original")
        elif mutation == "registration":
            await db.rollback_registration_sync("alice", ("original", "invite", False))
        else:
            await db.increment_usage("alice", regular=-5)

    with pytest.raises(DatabaseError):
        await mutate()

    user = await db.get_user("alice")
    assert user is not None
    assert user["bw_used"] == 10
    assert await db.notification_markers() == {marker}
    if mutation == "registration":
        assert await db.registration_identity("alice") == ("original", "invite", False)
        assert await db.get_code("invite") is None
