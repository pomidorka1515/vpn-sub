from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from bwatch.watch import BWatch
from db import Database
from notify import Kind, classify
from notify.notifier import Notifier
from tests.support.subscriptions import close_subscription, make_subscription
from tests.support.users import USER_UUID

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["expiry", "traffic", "whitelist", "combined"])
@pytest.mark.parametrize("restart", [False, True])
async def test_terminal_and_recovery_notifications_retry(
    tmp_path: Path,
    reason: str,
    restart: bool,
) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_subscription(db, tmp_path)
    send = AsyncMock(side_effect=RuntimeError("Telegram offline"))
    notifier = Notifier(
        texts={"en": {kind.text_key: kind.kind_id for kind in Kind}},
        send=send,
        language=AsyncMock(return_value="en"),
    )
    watch = BWatch(sub.res.cfg, db, sub, notifier=notifier, scheduled=False)
    expired = reason in {"expiry", "combined"}
    exhausted = reason in {"traffic", "combined"}
    wl_exhausted = reason in {"whitelist", "combined"}
    terminal_count = int(expired) + int(exhausted) + int(wl_exhausted)
    recovery_count = int(expired or exhausted) + int(wl_exhausted)
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            expires_at=int(time.time()) - 1 if expired else 0,
            bw_limit_gb=1 if exhausted else 0,
            wl_limit_gb=1 if wl_exhausted else 0,
        )
        await db.set_telegram("alice", "7")
        await db.increment_usage(
            "alice",
            regular=10**9 if exhausted else 0,
            whitelist=10**9 if wl_exhausted else 0,
        )
        await watch.check()
        assert send.await_count == terminal_count
        assert not await db.notification_markers()
        state = await db.get_user("alice")
        assert state is not None
        assert not classify(state, int(time.time()))
        if restart:
            await watch.close()
            watch = BWatch(sub.res.cfg, db, sub, notifier=notifier, scheduled=False)
        await watch.check()
        assert send.await_count == 2 * terminal_count
        send.side_effect = None
        await asyncio.gather(watch.check(), watch.check())
        assert send.await_count == 3 * terminal_count
        assert len(await db.notification_markers()) == terminal_count
        assert await db.get_metadata("user_notifications_pending:alice") is None

        await db.update_user("alice", expiry_time=0, bw_used=0, wl_bw_used=0)
        send.reset_mock()
        send.side_effect = RuntimeError("Telegram offline")
        await watch.bandwidth_check()
        assert send.await_count == recovery_count
        state = await db.get_user("alice")
        assert state is not None
        assert state["enabled"]
        assert state["enabled_time"]
        assert state["enabled_wl"]
        if restart:
            await watch.close()
            watch = BWatch(sub.res.cfg, db, sub, notifier=notifier, scheduled=False)
        await watch.bandwidth_check()
        assert send.await_count == 2 * recovery_count
        send.side_effect = None
        await watch.check()
        await watch.bandwidth_check()
        await watch.check()
        assert send.await_count == 3 * recovery_count
        assert await db.get_metadata("user_notifications_pending:alice") is None
        markers = await db.notification_markers()
        assert ("alice:restored" in markers) is (expired or exhausted)
        assert ("alice:whitelist_restored" in markers) is wl_exhausted
    finally:
        await watch.close()
        await close_subscription(sub)
        await db.close()


@pytest.mark.asyncio
async def test_notice_failure_and_concurrent_delivery(tmp_path: Path) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1,
            wl_limit_gb=0,
        )
        await db.increment_usages({"alice": (10**9, 0)})
        state = await db.get_user("alice")
        assert state is not None
        notice = classify(state, 0)[0]
        calls = 0

        async def send(_id: int, _text: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("offline")
            await asyncio.sleep(0)

        async def language(_id: int) -> str:
            return "en"

        notifier = Notifier(
            texts={"en": {notice.kind.text_key: "warning"}},
            send=send,
            language=language,
        )
        seen: set[str] = set()
        assert not await notifier.deliver("alice", 1, notice, seen)
        assert not seen
        assert (
            sum(
                await asyncio.gather(
                    *(notifier.deliver("alice", 1, notice, seen) for _ in range(2)),
                ),
            )
            == 1
        )
        assert calls == 2
    finally:
        await db.close()
