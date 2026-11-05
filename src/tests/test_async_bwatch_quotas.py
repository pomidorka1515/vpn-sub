from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from async_core_helpers import close_async_subscription, make_async_subscription
from helpers import USER_UUID, FakePanel, make_inbound

from bwatch.watch import BWatch
from db import Database
from notify import Kind, classify

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("whitelist", [False, True])
@pytest.mark.parametrize("exhausted", [False, True])
async def test_poll_preserves_explicit_disable(
    tmp_path: Path,
    whitelist: bool,
    exhausted: bool,
) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    notices = AsyncMock()
    watch._deliver = notices  # type: ignore[method-assign]
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1,
            wl_limit_gb=1,
        )
        if exhausted:
            await db.increment_usage("alice", regular=10**9, whitelist=10**9)
            await watch.check()
        await sub.business_svc.update_user(
            "alice",
            enable=None if whitelist else False,
            wl_enable=False if whitelist else None,
        )
        # Recovery after a reset, including a fresh watcher, must respect intent.
        await db.update_user("alice", bw_used=0, wl_bw_used=0)
        await watch.close()
        watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
        watch._deliver = notices  # type: ignore[method-assign]
        notices.reset_mock()
        await watch.bandwidth_check()
        state = await db.get_user("alice")
        assert state is not None
        assert not state["enabled_wl" if whitelist else "enabled"]
        assert state["enabled" if whitelist else "enabled_wl"]
        blocked_notice = Kind.WHITELIST_RESTORED if whitelist else Kind.RESTORED
        for call in notices.await_args_list:
            assert all(notice.kind is not blocked_notice for notice in call.args[1])
        # The shared quota helper must obey the same rule as polling.
        await sub.business_svc.apply_quota("alice")
        state = await db.get_user("alice")
        assert state is not None
        assert not state["enabled_wl" if whitelist else "enabled"]
        await sub.business_svc.update_user(
            "alice",
            enable=None if whitelist else True,
            wl_enable=True if whitelist else None,
        )
        await watch.bandwidth_check()
        state = await db.get_user("alice")
        assert state is not None
        assert state["enabled"]
        assert state["enabled_wl"]
    finally:
        await watch.close()
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["expiry", "traffic", "whitelist", "combined"])
@pytest.mark.parametrize("restart", [False, True])
async def test_quota_check_retries_failed_disable_without_terminal_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    restart: bool,
) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    main = FakePanel(name="main", inbounds=[make_inbound(1)])
    whitelist = FakePanel(name="whitelist", mode="whitelist", inbounds=[make_inbound(2)])
    sub = await make_async_subscription(db, tmp_path, panels=[main], whitelist=whitelist)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    expired = reason in {"expiry", "combined"}
    exhausted = reason in {"traffic", "combined"}
    wl_exhausted = reason in {"whitelist", "combined"}
    failed_panel = whitelist if reason == "whitelist" else main
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
        await sub.business_svc.add_users("alice")
        await db.increment_usage(
            "alice",
            regular=10**9 if exhausted else 0,
            whitelist=10**9 if wl_exhausted else 0,
        )
        monkeypatch.setattr(failed_panel, "_post_payload", {"success": False, "msg": "offline"})
        await watch.check()
        state = await db.get_user("alice")
        assert state is not None
        assert bool(state["enabled"]) is not (expired or exhausted)
        assert bool(state["enabled_time"]) is not expired
        assert bool(state["enabled_wl"]) is not wl_exhausted
        assert not classify(state, int(time.time()))
        assert await db.get_metadata("user_sync_pending:alice") is not None
        assert failed_panel.clients[0].enable

        if restart:
            await watch.close()
            watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
        reconcile = AsyncMock(wraps=sub.business_svc.reconcile_user)
        monkeypatch.setattr(sub.business_svc, "reconcile_user", reconcile)
        # A second failed check must retain the marker for another retry.
        await watch.check()
        assert reconcile.await_count == 1
        assert await db.get_metadata("user_sync_pending:alice") is not None
        assert failed_panel.clients[0].enable

        monkeypatch.setattr(failed_panel, "_post_payload", None)
        await watch.check()
        assert reconcile.await_count == 2
        assert await db.get_metadata("user_sync_pending:alice") is None
        assert main.clients[0].enable is not (expired or exhausted)
        assert whitelist.clients[0].enable is not wl_exhausted
        await watch.check()
        assert reconcile.await_count == 2
    finally:
        await watch.close()
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
async def test_quota_disable_recovers_after_reset(tmp_path: Path) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1,
            wl_limit_gb=1,
        )
        await db.increment_usage("alice", regular=10**9, whitelist=10**9)
        await watch.check()
        state = await db.get_user("alice")
        assert state is not None
        assert not state["enabled"]
        assert not state["enabled_wl"]
        assert not state["admin_disabled"]
        assert not state["admin_disabled_wl"]
        await db.update_user("alice", bw_used=0, wl_bw_used=0)
        await watch.bandwidth_check()
        state = await db.get_user("alice")
        assert state is not None
        assert state["enabled"]
        assert state["enabled_wl"]
    finally:
        await watch.close()
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("expiry_offset", [-1, 0, 1, None])
@pytest.mark.parametrize("exhausted", [False, True])
@pytest.mark.parametrize("whitelist_exhausted", [False, True])
async def test_quota_applies_combined_terminal_state_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expiry_offset: int | None,
    exhausted: bool,
    whitelist_exhausted: bool,
) -> None:
    now = int(time.time())
    monkeypatch.setattr("bwatch.watch.time.time", lambda: now)
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    update = AsyncMock(wraps=sub.business_svc.update_user)
    monkeypatch.setattr(sub.business_svc, "update_user", update)
    notices = AsyncMock()
    watch._deliver = notices  # type: ignore[method-assign]
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1,
            wl_limit_gb=1,
            expires_at=now + expiry_offset if expiry_offset is not None else 0,
        )
        await db.increment_usage(
            "alice",
            regular=10**9 if exhausted else 0,
            whitelist=10**9 if whitelist_exhausted else 0,
        )
        await watch.check()
        expired = expiry_offset is not None and expiry_offset <= 0
        state = await db.get_user("alice")
        assert state is not None
        assert bool(state["enabled"]) is not (expired or exhausted)
        assert bool(state["enabled_time"]) is not expired
        assert bool(state["enabled_wl"]) is not whitelist_exhausted
        expected_terminal: set[Kind] = set()
        if expired:
            expected_terminal.add(Kind.EXPIRED)
        if exhausted:
            expected_terminal.add(Kind.TRAFFIC_EXHAUSTED)
        if whitelist_exhausted:
            expected_terminal.add(Kind.WHITELIST_EXHAUSTED)
        assert update.await_count == int(bool(expected_terminal))
        delivered = {notice.kind for notice in notices.await_args_list[0].args[1]}
        assert (
            delivered
            & {
                Kind.EXPIRED,
                Kind.TRAFFIC_EXHAUSTED,
                Kind.WHITELIST_EXHAUSTED,
            }
            == expected_terminal
        )
        await watch.bandwidth_check()
        await watch.check()
        state = await db.get_user("alice")
        assert state is not None
        assert bool(state["enabled"]) is not (expired or exhausted)
        assert bool(state["enabled_time"]) is not expired
        assert update.await_count == int(bool(expected_terminal))
    finally:
        await watch.close()
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("expiry_offset", [-1, 0, 1, None])
async def test_recovery_only_restores_unexpired_accounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expiry_offset: int | None,
) -> None:
    now = int(time.time())
    monkeypatch.setattr("bwatch.watch.time.time", lambda: now)
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            expires_at=now + expiry_offset if expiry_offset is not None else 0,
            bw_limit_gb=10,
            wl_limit_gb=0,
        )
        await watch.check()
        # Also cover recovery from a traffic disable on a valid subscription.
        await db.update_user("alice", status=False, status_time=False)
        state = await db.get_user("alice")
        assert state is not None
        assert not state["enabled"]
        assert not state["enabled_time"]
        for _ in range(2):
            await watch.bandwidth_check()
            await watch.check()
            state = await db.get_user("alice")
            assert state is not None
            expected = expiry_offset is None or expiry_offset > 0
            assert bool(state["enabled"]) is expected
            assert bool(state["enabled_time"]) is expected
    finally:
        await watch.close()
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
async def test_quota_rereads_limits_after_waiting_for_admin(tmp_path: Path) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    entered, release = asyncio.Event(), asyncio.Event()
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

        async def admin() -> None:
            entered.set()
            await release.wait()
            await sub.business_svc.update_params("alice", limit=2)

        editing = asyncio.create_task(sub.res.coordinator.run(["alice"], admin, name="test:admin"))
        await entered.wait()
        checking = asyncio.create_task(watch.check())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(editing, checking)
        state = await db.get_user("alice")
        assert state is not None
        assert state["bw_limit_gb"] == 2
        assert state["enabled"]
    finally:
        await watch.close()
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
async def test_expiry_recovery_keeps_exhausted_main_disabled(tmp_path: Path) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1,
        )
        await db.increment_usage("alice", regular=10**9)
        await db.update_user("alice", status=False, status_time=False)
        await watch.bandwidth_check()
        state = await db.get_user("alice")
        assert state is not None
        assert not state["enabled"]
        assert state["enabled_time"]
    finally:
        await watch.close()
        await close_async_subscription(sub)
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_unlimited_quotas_restore_access(tmp_path: Path, expired: bool) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    watch = BWatch(sub.res.cfg, db, sub, scheduled=False)
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1,
            wl_limit_gb=1,
            expires_at=int(time.time()) - 1 if expired else 0,
        )
        await db.increment_usage("alice", regular=10**9, whitelist=10**9)
        await db.update_user("alice", status=False, status_wl=False)
        await sub.business_svc.update_params("alice", limit=0, wl_limit=0)
        await watch.bandwidth_check()
        state = await db.get_user("alice")
        assert state is not None
        assert bool(state["enabled"]) is not expired
        assert state["enabled_wl"]
    finally:
        await watch.close()
        await close_async_subscription(sub)
        await db.close()
