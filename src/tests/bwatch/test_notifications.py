from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from bwatch.watch import BWatch
from db import Database
from db.common import Connection
from notify import Kind, classify
from notify.notifier import Notifier
from tests.support.panels import FakePanel, make_inbound
from tests.support.subscriptions import close_subscription, make_subscription
from tests.support.users import USER_UUID

if TYPE_CHECKING:
    from pathlib import Path

    from db.common import Parameters


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["expiry", "traffic", "whitelist", "combined"])
@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize(
    "boundary",
    ["before_intent", "after_intent", "database_failure", "after_commit", "panel_failure", "after_sync"],
)
async def test_transition_intent_survives_interruption_and_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    recover: bool,
    boundary: str,
) -> None:
    path = tmp_path / "state.db"
    db = Database(path=path, start_backup=False)
    await db.start()
    main = FakePanel(name="main", inbounds=[make_inbound(1)])
    whitelist = FakePanel(name="whitelist", mode="whitelist", inbounds=[make_inbound(2)])
    sub = await make_subscription(db, tmp_path, panels=[main], whitelist_panel=whitelist)
    send = AsyncMock()
    notifier = Notifier(
        texts={"en": {kind.text_key: kind.kind_id for kind in Kind}},
        send=send,
        language=AsyncMock(return_value="en"),
    )
    watch = BWatch(sub.res.cfg, db, sub, notifier=notifier, scheduled=False)
    expired = reason in {"expiry", "combined"}
    exhausted = reason in {"traffic", "combined"}
    wl_exhausted = reason in {"whitelist", "combined"}
    expected = {
        kind
        for kind, applies in (
            ("restored", recover and (expired or exhausted)),
            ("whitelist_restored", recover and wl_exhausted),
            ("expired", not recover and expired),
            ("traffic_exhausted", not recover and exhausted),
            ("whitelist_exhausted", not recover and wl_exhausted),
        )
        if applies
    }
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
        await sub.business_svc.add_users("alice")
        await db.increment_usage(
            "alice", regular=10**9 if exhausted else 0, whitelist=10**9 if wl_exhausted else 0
        )
        if recover:
            await watch._quota("alice", recover=False)
            await db.update_user("alice", expiry_time=0, bw_used=0, wl_bw_used=0)
            send.reset_mock()
        before = await db.get_user("alice")
        markers = await db.notification_markers()
        execute = Connection.execute

        async def interrupt(conn: Connection, sql: str, parameters: Parameters = ()) -> int:
            if tuple(parameters)[:1] == ("user_notifications_pending:alice",):
                if boundary in {"after_intent", "database_failure"}:
                    await execute(conn, sql, parameters)
                if boundary == "database_failure":
                    raise sqlite3.OperationalError("outbox write failed")
                raise asyncio.CancelledError
            return await execute(conn, sql, parameters)

        with monkeypatch.context() as patch:
            if boundary in {"before_intent", "after_intent", "database_failure"}:
                patch.setattr(Connection, "execute", interrupt)
            elif boundary == "after_commit":
                patch.setattr(
                    sub.business_svc, "_sync_enabled", AsyncMock(side_effect=asyncio.CancelledError)
                )
            elif boundary == "after_sync":
                patch.setattr(watch, "_deliver", AsyncMock(side_effect=asyncio.CancelledError))
            else:
                failed = whitelist if reason == "whitelist" else main
                patch.setattr(failed, "_post_payload", {"success": False, "msg": "offline"})
            if boundary in {"panel_failure", "database_failure"}:
                await watch._quota("alice", recover=recover)
                # Reconciliation failure must not consume or send the outbox either.
                await watch._quota("alice", recover=recover)
            else:
                with pytest.raises(asyncio.CancelledError):
                    await watch._quota("alice", recover=recover)
        assert send.await_count == 0
        assert await db.notification_markers() == markers
        if boundary in {"before_intent", "after_intent", "database_failure"}:
            assert await db.get_user("alice") == before
            assert await db.get_metadata("user_notifications_pending:alice") is None
            assert await db.get_metadata("user_sync_pending:alice") is None
        else:
            assert await db.get_metadata("user_notifications_pending:alice") is not None
            state = await db.get_user("alice")
            assert state is not None
            assert bool(state["enabled"]) is (recover or not (expired or exhausted))
            assert bool(state["enabled_wl"]) is (recover or not wl_exhausted)

        # Reopen the actual database and runtime; no in-memory watcher state survives.
        await watch.close()
        await close_subscription(sub)
        await db.close()
        db = Database(path=path, start_backup=False)
        await db.start()
        sub = await make_subscription(db, tmp_path, panels=[main], whitelist_panel=whitelist)
        watch = BWatch(sub.res.cfg, db, sub, notifier=notifier, scheduled=False)
        await watch._quota("alice", recover=recover)
        assert {call.args[1] for call in send.await_args_list} == expected
        assert send.await_count == len(expected)
        assert await db.get_metadata("user_sync_pending:alice") is None
        assert await db.get_metadata("user_notifications_pending:alice") is None
        assert main.clients[0].enable is (recover or not (expired or exhausted))
        assert whitelist.clients[0].enable is (recover or not wl_exhausted)
        await watch._quota("alice", recover=recover)
        assert send.await_count == len(expected)
    finally:
        await watch.close()
        await close_subscription(sub)
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize("marker_committed", [False, True])
async def test_transition_send_is_at_least_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recover: bool,
    marker_committed: bool,
) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_subscription(db, tmp_path)
    send = AsyncMock()
    notifier = Notifier(
        texts={"en": {kind.text_key: kind.kind_id for kind in Kind}},
        send=send,
        language=AsyncMock(return_value="en"),
    )
    watch = BWatch(sub.res.cfg, db, sub, notifier=notifier, scheduled=False)
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1,
        )
        await db.set_telegram("alice", "7")
        await db.increment_usage("alice", regular=10**9)
        if recover:
            await watch._quota("alice", recover=False)
            await db.update_user("alice", bw_used=0)
            send.reset_mock()
        with monkeypatch.context() as patch:
            # Stop after remote acceptance, on either side of local acknowledgement.
            if marker_committed:
                # Only interrupt outbox cleanup, not successful panel-sync cleanup.
                delete = Database.delete_metadata

                async def cleanup(key: str) -> None:
                    if key == "user_notifications_pending:alice":
                        raise asyncio.CancelledError
                    await delete(db, key)

                patch.setattr(db, "delete_metadata", cleanup)
            else:
                patch.setattr(db, "sync_notifications", AsyncMock(side_effect=asyncio.CancelledError))
            with pytest.raises(asyncio.CancelledError):
                await watch._quota("alice", recover=recover)
        assert send.await_count == 1
        assert await db.get_metadata("user_notifications_pending:alice") is not None
        await watch.close()
        watch = BWatch(sub.res.cfg, db, sub, notifier=notifier, scheduled=False)
        await watch._quota("alice", recover=recover)
        assert send.await_count == (1 if marker_committed else 2)
        assert await db.get_metadata("user_notifications_pending:alice") is None
    finally:
        await watch.close()
        await close_subscription(sub)
        await db.close()


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
