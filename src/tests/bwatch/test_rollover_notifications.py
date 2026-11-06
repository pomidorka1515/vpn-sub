from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from bwatch.watch import BWatch
from db import Database
from notify import Kind, Notifier
from tests.support.panels import FakePanel, make_inbound
from tests.support.subscriptions import close_subscription, make_subscription
from tests.support.users import USER_UUID

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["traffic", "whitelist", "combined", "expiry", "expiry_whitelist"])
@pytest.mark.parametrize("delivered", [False, True])
@pytest.mark.parametrize("boundary", ["before_reset", "after_reset", "after_reenable"])
async def test_month_rollover_preserves_notification_episodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    delivered: bool,
    boundary: str,
) -> None:
    month = 9

    class Clock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> Clock:
            return cls(2026, month, 1, tzinfo=UTC)

    monkeypatch.setattr("bwatch.usage.datetime", Clock)
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
    expired = reason in {"expiry", "expiry_whitelist"}
    exhausted = reason in {"traffic", "combined"}
    wl_exhausted = reason in {"whitelist", "combined", "expiry_whitelist"}
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=1 if exhausted else 0,
            wl_limit_gb=1 if wl_exhausted else 0,
        )
        await db.set_telegram("alice", "7")
        await sub.business_svc.add_users("alice")
        await watch.bandwidth_check()
        await db.update_user("alice", expiry_time=int(time.time()) - 1 if expired else 0)
        await db.increment_usage(
            "alice", regular=10**9 if exhausted else 0, whitelist=10**9 if wl_exhausted else 0
        )
        if not delivered:
            send.side_effect = RuntimeError("Telegram offline")
        await watch.check()
        assert bool(await db.notification_markers()) is delivered
        send.side_effect = None
        send.reset_mock()
        month = 10

        increment = db.increment_usages

        async def reset(
            updates: Mapping[str, tuple[int, int]],
            *,
            baselines: str | None = None,
            period: tuple[str, str] | None = None,
        ) -> None:
            if boundary == "after_reset":
                await increment(updates, baselines=baselines, period=period)
            raise asyncio.CancelledError

        with monkeypatch.context() as patch:
            if boundary == "after_reenable":
                patch.setattr(watch, "_deliver", AsyncMock(side_effect=asyncio.CancelledError))
            else:
                patch.setattr(db, "increment_usages", reset)
            with pytest.raises(asyncio.CancelledError):
                await watch.bandwidth_check()
        assert send.await_count == 0
        if boundary != "before_reset":
            assert await db.get_metadata("last_reset_month") == "2026-10"
            pending = await db.get_metadata("user_notifications_pending:alice") or ""
            assert "TRAFFIC_EXHAUSTED" not in pending
            assert "WHITELIST_EXHAUSTED" not in pending

        await watch.close()
        await close_subscription(sub)
        await db.close()
        db = Database(path=path, start_backup=False)
        await db.start()
        sub = await make_subscription(db, tmp_path, panels=[main], whitelist_panel=whitelist)
        watch = BWatch(sub.res.cfg, db, sub, notifier=notifier, scheduled=False)
        if boundary != "before_reset":
            # A quota check can win the race against the next recovery poll.
            await watch.check()
        await watch.bandwidth_check()
        state = await db.get_user("alice")
        assert state is not None
        assert bool(state["enabled"]) is not expired
        assert state["enabled_wl"]
        assert main.clients[0].enable is not expired
        assert whitelist.clients[0].enable
        expected: list[str] = []
        if expired and not delivered:
            expected.append("expired")
        if exhausted and delivered:
            expected.append("restored")
        if wl_exhausted and delivered:
            expected.append("whitelist_restored")
        assert sorted(call.args[1] for call in send.await_args_list) == sorted(expected)

        if expired:
            # Expiry evidence survives rollover even while the account stays expired.
            assert "alice:expired:1" in await db.notification_markers()
            await db.update_user("alice", expiry_time=0)
            await watch.bandwidth_check()
            expected.append("restored")
        await watch.check()
        await watch.bandwidth_check()
        assert sorted(call.args[1] for call in send.await_args_list) == sorted(expected)
        assert await db.get_metadata("user_notifications_pending:alice") is None
    finally:
        await watch.close()
        await close_subscription(sub)
        await db.close()
