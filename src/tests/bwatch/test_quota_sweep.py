from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from bwatch.watch import BWatch
from custom_types import BandwidthInfo
from notify import Kind, Notifier
from tests.support.subscriptions import close_subscription, make_subscription
from tests.support.users import create_alice

if TYPE_CHECKING:
    from db import Database


@pytest.mark.asyncio
@pytest.mark.parametrize("notifications", [False, True])
async def test_quiet_sweep_has_constant_database_calls_at_10k_users(
    database: Database, monkeypatch: pytest.MonkeyPatch, notifications: bool
) -> None:
    # Active, expired, exhausted and explicitly disabled accounts are all no-ops.
    now = int(time.time())
    async with database.transaction(immediate=True) as conn:
        await conn.executemany(
            "INSERT INTO users (username, uuid, token, fingerprint, displayname, "
            "bw_limit_gb, bw_used, expires_at, enabled, enabled_time, admin_disabled) "
            "VALUES (?, ?, ?, 'chrome', 'User', 1, ?, ?, ?, ?, ?)",
            [
                (
                    f"user{number:05}",
                    f"00000000-0000-0000-0000-{number:012x}",
                    f"{number:040x}",
                    10**9 if number % 4 == 2 else 0,
                    now - 1 if number % 4 == 1 else 0,
                    int(number % 4 == 0),
                    int(number % 4 != 1),
                    int(number % 4 == 3),
                )
                for number in range(10_000)
            ],
        )
    sub = await make_subscription(database)
    notifier = (
        Notifier(texts={}, send=AsyncMock(), language=AsyncMock(return_value="en"))
        if notifications
        else None
    )
    watch = BWatch(sub.res.cfg, database, sub, notifier=notifier, scheduled=False)
    traffic = AsyncMock(return_value={"panel": {"user00000": BandwidthInfo(100, 0, 100)}})
    monkeypatch.setattr(sub.bandwidth_svc, "panel_traffic", traffic)
    try:
        await watch.bandwidth_check()
        quota = AsyncMock(wraps=watch._quota)
        get_user = AsyncMock(wraps=database.get_user)
        metadata = AsyncMock(wraps=database.get_metadata)
        bulk_metadata = AsyncMock(wraps=database.list_metadata)
        states = AsyncMock(wraps=sub.user_svc.list_user_states)
        increment = AsyncMock(wraps=database.increment_usages)
        monkeypatch.setattr(watch, "_quota", quota)
        monkeypatch.setattr(database, "get_user", get_user)
        monkeypatch.setattr(database, "get_metadata", metadata)
        monkeypatch.setattr(database, "list_metadata", bulk_metadata)
        monkeypatch.setattr(sub.user_svc, "list_user_states", states)
        monkeypatch.setattr(database, "increment_usages", increment)
        for total, updates in ((100, {}), (140, {"user00000": (40, 0)}), (140, {})):
            traffic.return_value = {"panel": {"user00000": BandwidthInfo(total, 0, total)}}
            await watch.bandwidth_check()
            quota.assert_not_awaited()
            get_user.assert_not_awaited()
            assert states.await_count == 3
            assert [call.args[0] for call in metadata.await_args_list] == [
                "last_reset_month",
                "watcher_usage_baselines",
            ]
            assert [call.args[0] for call in bulk_metadata.await_args_list] == [
                "user_sync_pending:",
                *(["user_notifications_pending:"] if notifications else []),
            ]
            assert increment.await_args is not None
            assert increment.await_args.args[0] == updates
            states.reset_mock()
            metadata.reset_mock()
            bulk_metadata.reset_mock()
        await watch.check()
        quota.assert_not_awaited()
        get_user.assert_not_awaited()
        metadata.assert_not_awaited()
        states.assert_awaited_once()
        assert [call.args[0] for call in bulk_metadata.await_args_list] == [
            "user_sync_pending:",
            *(["user_notifications_pending:"] if notifications else []),
        ]
    finally:
        await watch.close()
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_check_selects_warnings_terminals_and_pending_sync(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    for number, (name, used) in enumerate(
        (("quiet", 0), ("warning", 810_000_000), ("terminal", 10**9), ("sync", 0)), 1
    ):
        await database.create_user(
            username=name,
            uuid=f"00000000-0000-4000-8000-{number:012d}",
            token=f"{number:040d}",
            fingerprint="chrome",
            displayname=name,
            bw_limit_gb=1,
        )
        await database.increment_usage(name, regular=used)
    await database.set_metadata("user_sync_pending:sync", "reconcile")
    sub = await make_subscription(database)
    watch = BWatch(sub.res.cfg, database, sub, scheduled=False)
    quota = AsyncMock(wraps=watch._quota)
    monkeypatch.setattr(watch, "_quota", quota)
    try:
        await watch.check()
        assert {call.args[0] for call in quota.await_args_list} == {"warning", "terminal", "sync"}
        assert all(call.kwargs == {"recover": False} for call in quota.await_args_list)
        state = await database.get_user("terminal")
        assert state is not None
        assert not state["enabled"]
        assert await database.get_metadata("user_sync_pending:sync") is None
    finally:
        await watch.close()
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_check_candidate_is_revalidated_after_admin_edit(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await create_alice(database, bw_limit_gb=1)
    await database.increment_usage("alice", regular=10**9)
    sub = await make_subscription(database)
    watch = BWatch(sub.res.cfg, database, sub, scheduled=False)
    quota = watch._quota

    async def admin_then_quota(username: str, *, recover: bool) -> None:
        await sub.business_svc.update_params(username, limit=2)
        await quota(username, recover=recover)

    selected = AsyncMock(side_effect=admin_then_quota)
    monkeypatch.setattr(watch, "_quota", selected)
    try:
        await watch.check()
        selected.assert_awaited_once_with("alice", recover=False)
        state = await database.get_user("alice")
        assert state is not None
        assert state["enabled"]
        assert state["bw_limit_gb"] == 2
    finally:
        await watch.close()
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_check_retries_quiet_outbox_without_duplicate_attempts(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await create_alice(database, bw_limit_gb=1)
    await database.set_telegram("alice", "7")
    await database.increment_usage("alice", regular=10**9)
    sub = await make_subscription(database)
    send = AsyncMock(side_effect=RuntimeError("offline"))
    notifier = Notifier(
        texts={"en": {kind.text_key: kind.kind_id for kind in Kind}},
        send=send,
        language=AsyncMock(return_value="en"),
    )
    watch = BWatch(sub.res.cfg, database, sub, notifier=notifier, scheduled=False)
    scan = watch._scan_notifications

    async def drain_then_scan(*, exclude: set[str] | None = None) -> None:
        await watch.drain_notifications()
        await scan(exclude=exclude)

    monkeypatch.setattr(watch, "_scan_notifications", drain_then_scan)
    try:
        await watch.check()
        assert send.await_count == 1
        assert await database.get_metadata("user_notifications_pending:alice") is not None
        send.side_effect = None
        await watch.check()
        await watch.drain_notifications()
        assert send.await_count == 2
        assert await database.get_metadata("user_notifications_pending:alice") is None
    finally:
        await watch.close()
        await close_subscription(sub)


@pytest.mark.asyncio
@pytest.mark.parametrize("whitelist", [False, True])
async def test_recovery_selection_uses_committed_usage(
    database: Database, monkeypatch: pytest.MonkeyPatch, whitelist: bool
) -> None:
    await create_alice(database, bw_limit_gb=1, wl_limit_gb=1)
    sub = await make_subscription(database)
    watch = BWatch(sub.res.cfg, database, sub, scheduled=False)
    traffic = AsyncMock(return_value={"panel": {"alice": BandwidthInfo(100, 0, 100)}})
    monkeypatch.setattr(sub.bandwidth_svc, "panel_traffic", traffic)
    try:
        await watch.bandwidth_check()
        await database.update_user(
            "alice",
            status=whitelist,
            status_wl=not whitelist,
            bw_used=0 if whitelist else 10**9 - 10,
            wl_bw_used=10**9 - 10 if whitelist else 0,
        )
        quota = AsyncMock(wraps=watch._quota)
        monkeypatch.setattr(watch, "_quota", quota)
        traffic.return_value = {"panel": {"alice": BandwidthInfo(120, 0, 120)}}
        await watch.bandwidth_check()
        quota.assert_not_awaited()
        state = await database.get_user("alice")
        assert state is not None
        assert state["wl_used" if whitelist else "bw_used"] == 10**9 + 10
        assert not state["enabled_wl" if whitelist else "enabled"]
    finally:
        await watch.close()
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_recovery_candidate_is_revalidated_after_admin_edit(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await create_alice(database)
    await database.update_user("alice", status=False)
    sub = await make_subscription(database)
    watch = BWatch(sub.res.cfg, database, sub, scheduled=False)
    quota = watch._quota

    async def admin_then_quota(username: str, *, recover: bool) -> None:
        await sub.business_svc.update_user(username, enable=False)
        await quota(username, recover=recover)

    selected = AsyncMock(side_effect=admin_then_quota)
    monkeypatch.setattr(watch, "_quota", selected)
    try:
        await watch.bandwidth_check()
        selected.assert_awaited_once_with("alice", recover=True)
        state = await database.get_user("alice")
        assert state is not None
        assert state["admin_disabled"]
        assert not state["enabled"]
    finally:
        await watch.close()
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_recovery_outbox_is_not_retried_twice_in_one_sweep(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await create_alice(database, bw_limit_gb=1)
    await database.set_telegram("alice", "7")
    await database.increment_usage("alice", regular=10**9)
    sub = await make_subscription(database)
    send = AsyncMock()
    notifier = Notifier(
        texts={"en": {kind.text_key: kind.kind_id for kind in Kind}},
        send=send,
        language=AsyncMock(return_value="en"),
    )
    watch = BWatch(sub.res.cfg, database, sub, notifier=notifier, scheduled=False)
    try:
        await watch.check()
        await watch.drain_notifications()
        await database.update_user("alice", bw_used=0)
        send.reset_mock()
        send.side_effect = RuntimeError("offline")
        scan = watch._scan_notifications

        async def drain_then_scan(*, exclude: set[str] | None = None) -> None:
            # Force completion before the bulk scan, exposing duplicate admission.
            await watch.drain_notifications()
            await scan(exclude=exclude)

        monkeypatch.setattr(watch, "_scan_notifications", drain_then_scan)
        await watch.bandwidth_check()
        assert send.await_count == 1
        assert await database.get_metadata("user_notifications_pending:alice") is not None
    finally:
        await watch.close()
        await close_subscription(sub)
