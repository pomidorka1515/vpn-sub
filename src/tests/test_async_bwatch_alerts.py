from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from async_core_helpers import close_async_subscription, make_async_subscription
from bot_helpers import telegram_mock
from helpers import USER_UUID, config_mock, subscription_config

from bots.telegram import TelegramAdminBot
from bwatch.watch import BWatch
from db import Database
from notify.notifier import Notifier

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize(("cooldown", "expected_alerts"), [(0, 2), (3600, 1)])
async def test_panel_alerts_respect_configured_cooldown(
    watch: BWatch,
    monkeypatch: pytest.MonkeyPatch,
    cooldown: int,
    expected_alerts: int,
) -> None:
    watch.cfg = config_mock(subscription_config(panel_alert_cooldown=cooldown))
    status = MagicMock()
    status.obj.xray.state = "running"
    status.obj.cpu = 95
    status.obj.mem.total = 0
    status.obj.disk.total = 0
    monkeypatch.setattr(watch.sub.panel_svc, "statuses", AsyncMock(return_value=[status]))
    alert = AsyncMock()
    watch.alert = alert
    now = 100.0
    monkeypatch.setattr("bwatch.watch.time", SimpleNamespace(monotonic=lambda: now))

    await watch.panel_health_check()
    alert.assert_awaited_once()
    now += 1
    await watch.panel_health_check()

    assert alert.await_count == expected_alerts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("plain text\nnext line", "plain text\nnext line"),
        ("snapshot: <disk> & failed", "snapshot: &lt;disk&gt; &amp; failed"),
        ('Xray: <invalid & "broken">', "Xray: &lt;invalid &amp; &quot;broken&quot;&gt;"),
        ("Panel 'a<b>':", "Panel &#x27;a&lt;b&gt;&#x27;:"),
        ("reconcile: alice&bob", "reconcile: alice&amp;bob"),
        ("literal &lt;tag&gt;", "literal &amp;lt;tag&amp;gt;"),
    ],
)
async def test_admin_alerts_escape_plain_text_for_telegram(
    watch: BWatch,
    message: str,
    expected: str,
) -> None:
    bot = TelegramAdminBot.__new__(TelegramAdminBot)
    telegram = telegram_mock()
    bot.bot = telegram
    bot.admin_uids = [42, 43]
    bot.log = MagicMock()
    watch.alert = bot.msg

    await watch._alert_admin(message)

    assert telegram.send_message.await_count == 2
    telegram.send_message.assert_any_await(42, expected, parse_mode="HTML")
    telegram.send_message.assert_any_await(43, expected, parse_mode="HTML")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("used", "disabled"),
    [(10**9 - 1, False), (10**9, True), (10**9 + 1, True)],
)
@pytest.mark.parametrize("whitelist", [False, True])
async def test_terminal_alert_uses_pre_disable_state_at_quota_boundary(
    tmp_path: Path,
    used: int,
    disabled: bool,
    whitelist: bool,
) -> None:
    db = Database(path=tmp_path / "state.db", start_backup=False)
    await db.start()
    sub = await make_async_subscription(db, tmp_path)
    sent: list[str] = []

    async def send(_id: int, text: str) -> None:
        sent.append(text)

    async def language(_id: int) -> str:
        return "en"

    notifier = Notifier(
        texts={"en": {"traffic_exhausted": "exhausted", "traffic_soon": "soon"}},
        send=send,
        language=language,
    )
    watch = BWatch(sub.res.cfg, db, sub, notifier=notifier, scheduled=False)
    try:
        await db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            bw_limit_gb=0 if whitelist else 1,
            wl_limit_gb=1 if whitelist else 0,
        )
        await db.increment_usages({"alice": (0, used) if whitelist else (used, 0)})
        state = await db.get_user("alice")
        assert state is not None
        from notify import classify

        notice = classify(state, 0)[0]
        notifier._texts = {"en": {notice.kind.text_key: "warning"}}
        # Deliver through the same path as polling without needing bot wiring.

        async def mappings() -> dict[str, str]:
            return {"alice": "1"}

        db.user_tgids = mappings  # type: ignore[method-assign]
        await watch.check()
        result = await db.get_user("alice")
        assert result is not None
        assert bool(result["enabled_wl" if whitelist else "enabled"]) is not disabled
        assert sent == ["warning"]
        assert f"alice:{notice.marker}" in await db.notification_markers()
        await watch.check()
        assert sent == ["warning"]
    finally:
        await watch.close()
        await close_async_subscription(sub)
        await db.close()
