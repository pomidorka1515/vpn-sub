from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from bwatch.watch import BWatch
from notify import Kind, Notifier
from tests.support.subscriptions import close_subscription, make_subscription
from tests.support.users import USER_UUID

if TYPE_CHECKING:
    from db import Database


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["main_topup", "main_reset", "wl_topup", "wl_reset", "expiry"])
async def test_warning_bucket_reopens_after_entitlement_change(
    database: Database, change: str
) -> None:
    sub = await make_subscription(database)
    send = AsyncMock()
    notifier = Notifier(
        texts={"en": {kind.text_key: kind.kind_id for kind in Kind}},
        send=send,
        language=AsyncMock(return_value="en"),
    )
    watch = BWatch(sub.res.cfg, database, sub, notifier=notifier, scheduled=False)
    try:
        expiry = int(time.time()) + 2 * 86400 + 120
        await database.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            expires_at=expiry if change == "expiry" else 0,
            bw_limit_gb=1 if change.startswith("main") else 0,
            wl_limit_gb=1 if change.startswith("wl") else 0,
        )
        await database.set_telegram("alice", "7")
        if change.startswith("main"):
            await database.increment_usage("alice", regular=810_000_000)
        elif change.startswith("wl"):
            await database.increment_usage("alice", whitelist=810_000_000)
        await watch._quota("alice", recover=False)
        await watch.drain_notifications()
        assert send.await_count == 1
        if change == "main_topup":
            await database.update_user("alice", bw_limit=2)
            await database.increment_usage("alice", regular=820_000_000)
        elif change == "main_reset":
            await database.update_user("alice", bw_used=0)
            await database.increment_usage("alice", regular=810_000_000)
        elif change == "wl_topup":
            await database.update_user("alice", wl_bw_limit=2)
            await database.increment_usage("alice", whitelist=820_000_000)
        elif change == "wl_reset":
            await database.update_user("alice", wl_bw_used=0)
            await database.increment_usage("alice", whitelist=810_000_000)
        else:
            await database.update_user("alice", expiry_time=expiry + 60)
        await watch.close()
        watch = BWatch(sub.res.cfg, database, sub, notifier=notifier, scheduled=False)
        await watch._quota("alice", recover=False)
        await watch.drain_notifications()
        assert send.await_count == 2
        assert send.await_args_list[0].args[1] == send.await_args_list[1].args[1]
    finally:
        await watch.close()
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_episode_changes_preserve_other_sides_and_outages(database: Database) -> None:
    await database.create_user(
        username="alice",
        uuid=USER_UUID,
        token="a" * 40,
        fingerprint="chrome",
        displayname="Alice",
        bw_limit_gb=1,
        wl_limit_gb=1,
    )
    initial = {"alice:traffic_soon:80", "alice:traffic_exhausted:1", "alice:whitelist_soon:80"}
    await database.sync_notifications(set(), initial)
    await database.update_user("alice", bw_limit=2)
    assert await database.notification_markers() == initial - {"alice:traffic_soon:80"}
    await database.delete_user("alice")
    await database.create_user(
        username="alice",
        uuid="00000000-0000-4000-8000-000000000002",
        token="b" * 40,
        fingerprint="chrome",
        displayname="Alice",
    )
    assert not await database.notification_markers()


@pytest.mark.asyncio
async def test_bonus_clears_only_affected_warning_buckets(database: Database) -> None:
    await database.create_user(
        username="alice",
        uuid=USER_UUID,
        token="a" * 40,
        fingerprint="chrome",
        displayname="Alice",
        bw_limit_gb=1,
        wl_limit_gb=1,
    )
    markers = {"alice:traffic_soon:80", "alice:whitelist_soon:80", "alice:traffic_exhausted:1"}
    await database.sync_notifications(set(), markers)
    await database.add_code("bonus", "bonus", gb=1)
    await database.adjust_bonus("alice", "bonus")
    assert await database.notification_markers() == markers - {"alice:traffic_soon:80"}


@pytest.mark.asyncio
async def test_uuid_change_retains_recovery_evidence_and_intent(database: Database) -> None:
    await database.create_user(
        username="alice",
        uuid=USER_UUID,
        token="a" * 40,
        fingerprint="chrome",
        displayname="Alice",
    )
    markers = {"alice:traffic_soon:80", "alice:traffic_exhausted:1"}
    await database.sync_notifications(set(), markers)
    await database.set_metadata(
        "user_notifications_pending:alice",
        json.dumps({"uuid": USER_UUID, "notices": [{"kind": "RESTORED"}]}),
    )
    new_uuid = "00000000-0000-4000-8000-000000000002"
    await database.update_user("alice", uuid=new_uuid)
    assert await database.notification_markers() == {"alice:traffic_exhausted:1"}
    raw = await database.get_metadata("user_notifications_pending:alice")
    assert raw is not None
    assert json.loads(raw) == {"uuid": new_uuid, "notices": [{"kind": "RESTORED"}]}
