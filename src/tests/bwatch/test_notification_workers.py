from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from bwatch.watch import BWatch
from notify import Kind, Notifier
from tests.support.subscriptions import close_subscription, make_subscription

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from db import Database


async def _user(db: Database, name: str, number: int, *, used: int = 810_000_000) -> None:
    await db.create_user(
        username=name,
        uuid=f"00000000-0000-4000-8000-{number:012d}",
        token=f"{number:040d}",
        fingerprint="chrome",
        displayname=name,
        bw_limit_gb=1,
    )
    await db.set_telegram(name, str(number))
    await db.increment_usage(name, regular=used)


def _notifier(
    send: Callable[[int, str], Awaitable[object]],
    language: Callable[[int], Awaitable[str]],
    *,
    timeout: float = 10,
) -> Notifier:
    return Notifier(
        texts={"en": {kind.text_key: kind.kind_id for kind in Kind}},
        send=send,
        language=language,
        timeout=timeout,
    )


@pytest.mark.asyncio
async def test_stalled_language_does_not_block_quota_or_mutations(database: Database) -> None:
    sub = await make_subscription(database)
    started, release, bob_sent = asyncio.Event(), asyncio.Event(), asyncio.Event()
    sent: list[int] = []

    async def language(tgid: int) -> str:
        if tgid == 1:
            started.set()
            await release.wait()
        return "en"

    async def send(tgid: int, _text: str) -> None:
        sent.append(tgid)
        if tgid == 2:
            bob_sent.set()

    watch = BWatch(sub.res.cfg, database, sub, notifier=_notifier(send, language), scheduled=False)
    try:
        await _user(database, "alice", 1)
        await _user(database, "bob", 2, used=10**9)
        await asyncio.wait_for(watch.check(), 3)
        await asyncio.wait_for(started.wait(), 3)
        await asyncio.wait_for(bob_sent.wait(), 3)
        bob = await database.get_user("bob")
        assert bob is not None
        assert not bob["enabled"]
        await asyncio.wait_for(sub.business_svc.update_params("alice", limit=2), 3)
        release.set()
        await asyncio.wait_for(watch.drain_notifications(), 3)
        assert sent == [2]
        assert await database.notification_markers() == {"bob:traffic_exhausted:1"}
    finally:
        await watch.close()
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_stalled_send_does_not_block_quota_or_ack_stale_warning(database: Database) -> None:
    sub = await make_subscription(database)
    started, release, bob_sent = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def send(tgid: int, _text: str) -> None:
        if tgid == 1:
            started.set()
            await release.wait()
        else:
            bob_sent.set()

    watch = BWatch(
        sub.res.cfg,
        database,
        sub,
        notifier=_notifier(send, AsyncMock(return_value="en")),
        scheduled=False,
    )
    try:
        await _user(database, "alice", 1)
        await _user(database, "bob", 2, used=10**9)
        await asyncio.wait_for(watch.check(), 3)
        await asyncio.wait_for(started.wait(), 3)
        await asyncio.wait_for(bob_sent.wait(), 3)
        await asyncio.wait_for(sub.business_svc.update_params("alice", limit=2), 3)
        release.set()
        await asyncio.wait_for(watch.drain_notifications(), 3)
        assert await database.notification_markers() == {"bob:traffic_exhausted:1"}
    finally:
        await watch.close()
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_shutdown_cancels_inflight_and_restart_retries(database: Database) -> None:
    sub = await make_subscription(database)
    started, cancelled = asyncio.Event(), asyncio.Event()
    calls: list[int] = []
    block = True

    async def send(tgid: int, _text: str) -> None:
        calls.append(tgid)
        if block:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    notifier = _notifier(send, AsyncMock(return_value="en"))
    watch = BWatch(sub.res.cfg, database, sub, notifier=notifier, scheduled=False)
    try:
        await _user(database, "alice", 1, used=10**9)
        await watch.check()
        await asyncio.wait_for(started.wait(), 3)
        await asyncio.wait_for(watch.close(), 3)
        assert cancelled.is_set()
        assert watch._notice_tasks.active_count == 0
        assert sub.res.coordinator.lock_count == 0
        assert await database.get_metadata("user_notifications_pending:alice") is not None
        assert not await database.notification_markers()
        block = False
        watch = BWatch(sub.res.cfg, database, sub, notifier=notifier, scheduled=False)
        await watch.start()
        await asyncio.wait_for(watch.drain_notifications(), 3)
        assert calls == [1, 1]
        assert await database.notification_markers() == {"alice:traffic_exhausted:1"}
        assert await database.get_metadata("user_notifications_pending:alice") is None
    finally:
        await watch.close()
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_bounded_workers_overflow_and_retry(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(BWatch, "_NOTICE_WORKERS", 2)
    monkeypatch.setattr(BWatch, "_NOTICE_QUEUE_SIZE", 1)
    sub = await make_subscription(database)
    active = peak = 0
    full, release = asyncio.Event(), asyncio.Event()
    sent: list[int] = []

    async def send(tgid: int, _text: str) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            full.set()
        try:
            await release.wait()
            sent.append(tgid)
        finally:
            active -= 1

    watch = BWatch(
        sub.res.cfg,
        database,
        sub,
        notifier=_notifier(send, AsyncMock(return_value="en")),
        scheduled=False,
    )
    try:
        for number in range(1, 7):
            await _user(database, f"user{number}", number)
        await watch.check()
        await asyncio.wait_for(full.wait(), 3)
        assert peak == 2
        assert len(await database.list_metadata("user_notifications_pending:")) == 6
        release.set()
        await asyncio.wait_for(watch.drain_notifications(), 3)
        for _ in range(6):
            await watch._scan_notifications()
            await asyncio.wait_for(watch.drain_notifications(), 3)
        assert sorted(sent) == list(range(1, 7))
        assert peak == 2
        assert not await database.list_metadata("user_notifications_pending:")
    finally:
        await watch.close()
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_recovery_waits_for_inflight_outage(database: Database) -> None:
    sub = await make_subscription(database)
    started, release = asyncio.Event(), asyncio.Event()
    sent: list[str] = []

    async def send(_tgid: int, text: str) -> None:
        sent.append(text)
        if text == "traffic_exhausted":
            started.set()
            await release.wait()

    watch = BWatch(
        sub.res.cfg,
        database,
        sub,
        notifier=_notifier(send, AsyncMock(return_value="en")),
        scheduled=False,
    )
    try:
        await _user(database, "alice", 1, used=10**9)
        await watch.check()
        await asyncio.wait_for(started.wait(), 3)
        await sub.res.coordinator.run(
            ["alice"], lambda: database.update_user("alice", bw_used=0), name="test:reset"
        )
        await asyncio.wait_for(watch._quota("alice", recover=True), 3)
        assert sent == ["traffic_exhausted"]
        release.set()
        await asyncio.wait_for(watch.drain_notifications(), 3)
        assert sent == ["traffic_exhausted", "restored"]
        assert await database.notification_markers() == {"alice:restored"}
    finally:
        await watch.close()
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_recreated_account_is_revalidated_after_language(database: Database) -> None:
    sub = await make_subscription(database)
    started, release = asyncio.Event(), asyncio.Event()
    send = AsyncMock()

    async def language(_tgid: int) -> str:
        started.set()
        await release.wait()
        return "en"

    watch = BWatch(sub.res.cfg, database, sub, notifier=_notifier(send, language), scheduled=False)
    try:
        await _user(database, "alice", 1)
        await watch.check()
        await asyncio.wait_for(started.wait(), 3)

        async def recreate() -> None:
            await database.delete_user("alice")
            await _user(database, "alice", 2)

        await asyncio.wait_for(
            sub.res.coordinator.run(["alice"], recreate, name="test:recreate"), 3
        )
        release.set()
        await asyncio.wait_for(watch.drain_notifications(), 3)
        send.assert_not_awaited()
        assert not await database.notification_markers()
    finally:
        await watch.close()
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_timeout_keeps_intent_for_retry(database: Database) -> None:
    sub = await make_subscription(database)
    calls = 0

    async def send(_tgid: int, _text: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.Event().wait()

    watch = BWatch(
        sub.res.cfg,
        database,
        sub,
        notifier=_notifier(send, AsyncMock(return_value="en"), timeout=0.05),
        scheduled=False,
    )
    try:
        await _user(database, "alice", 1, used=10**9)
        await watch.check()
        await asyncio.wait_for(watch.drain_notifications(), 3)
        assert calls == 1
        assert not await database.notification_markers()
        assert await database.get_metadata("user_notifications_pending:alice") is not None
        await watch._scan_notifications()
        await asyncio.wait_for(watch.drain_notifications(), 3)
        assert calls == 2
        assert await database.notification_markers() == {"alice:traffic_exhausted:1"}
    finally:
        await watch.close()
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_retry_loop_recovers_without_another_quota_poll(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(BWatch, "_NOTICE_RETRY_SECONDS", 0.05)
    sub = await make_subscription(database)
    retried = asyncio.Event()
    calls = 0

    async def send(_tgid: int, _text: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("offline")
        retried.set()

    watch = BWatch(
        sub.res.cfg,
        database,
        sub,
        notifier=_notifier(send, AsyncMock(return_value="en")),
        scheduled=False,
    )
    try:
        await _user(database, "alice", 1, used=10**9)
        await watch.check()
        await asyncio.wait_for(retried.wait(), 3)
        await asyncio.wait_for(watch.drain_notifications(), 3)
        assert calls == 2
        assert await database.notification_markers() == {"alice:traffic_exhausted:1"}
        assert await database.get_metadata("user_notifications_pending:alice") is None
    finally:
        await watch.close()
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_overflow_retry_is_fair_to_accounts_after_permanent_failures(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(BWatch, "_NOTICE_WORKERS", 1)
    monkeypatch.setattr(BWatch, "_NOTICE_QUEUE_SIZE", 1)
    monkeypatch.setattr(BWatch, "_NOTICE_RETRY_SECONDS", 0.05)
    sub = await make_subscription(database)
    last_sent = asyncio.Event()

    async def send(tgid: int, _text: str) -> None:
        if tgid <= 3:
            raise RuntimeError("offline")
        if tgid == 6:
            last_sent.set()

    watch = BWatch(
        sub.res.cfg,
        database,
        sub,
        notifier=_notifier(send, AsyncMock(return_value="en")),
        scheduled=False,
    )
    try:
        for number in range(1, 7):
            await _user(database, f"user{number}", number)
        await watch.check()
        await asyncio.wait_for(last_sent.wait(), 3)
        assert await database.get_metadata("user_notifications_pending:user1") is not None
    finally:
        await watch.close()
        await close_subscription(sub)
