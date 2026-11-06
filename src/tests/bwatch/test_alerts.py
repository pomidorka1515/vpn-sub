from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from bots.telegram import TelegramAdminBot
from bwatch.watch import BWatch
from db import Database
from notify.notifier import Notifier
from tests.support.config import config_mock, subscription_config
from tests.support.subscriptions import close_subscription, make_subscription
from tests.support.telegram import telegram_mock
from tests.support.users import USER_UUID

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
    monkeypatch.setattr("bwatch.panels.time", SimpleNamespace(monotonic=lambda: now))

    await watch.panel_health_check()
    alert.assert_awaited_once()
    now += 1
    await watch.panel_health_check()

    assert alert.await_count == expected_alerts


def panel_status(*, cpu: int = 0) -> MagicMock:
    status = MagicMock()
    status.obj.xray.state = "running"
    status.obj.cpu = cpu
    status.obj.mem.total = 0
    status.obj.disk.total = 0
    return status


@pytest.mark.asyncio
async def test_panel_health_unknown_dead_and_recovery(
    watch: BWatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watch.cfg = config_mock(subscription_config(panel_alert_cooldown=3600))
    panel = watch.sub.panels[0]
    healthy = panel_status()
    statuses = AsyncMock(return_value=[healthy])
    monkeypatch.setattr(watch.sub.panel_svc, "statuses", statuses)
    alert = AsyncMock()
    watch.alert = alert
    now = 100.0
    monkeypatch.setattr("bwatch.panels.time", SimpleNamespace(monotonic=lambda: now))

    await watch.panel_health_check()
    alert.assert_not_awaited()
    assert watch._panel_alerts == {}

    statuses.return_value = [None]
    await watch.panel_health_check()
    assert "Unknown: panel status could not be read" in alert.call_args.args[0]
    assert watch._panel_alerts == {panel.name: now}

    panel.dead = True
    statuses.reset_mock()
    now += 1
    await watch.panel_health_check()
    statuses.assert_not_awaited()
    assert alert.await_count == 1
    assert watch._panel_alerts == {panel.name: 100.0}

    now += 3600
    await watch.panel_health_check()
    statuses.assert_not_awaited()
    assert alert.await_count == 2
    assert "Unavailable: panel session is down" in alert.call_args.args[0]

    # A live session alone is not recovery: metrics must also be readable.
    panel.dead = False
    await watch.panel_health_check()
    assert watch._panel_alerts == {panel.name: now}
    statuses.return_value = [healthy]
    await watch.panel_health_check()
    assert watch._panel_alerts == {}
    assert alert.await_count == 2  # Recovery is silent.

    statuses.return_value = [None]
    await watch.panel_health_check()
    assert alert.await_count == 3  # A new outage is not suppressed by old cooldown.


@pytest.mark.asyncio
@pytest.mark.parametrize("dead", [False, True])
async def test_panel_outage_reminders_share_per_panel_cooldown(
    watch: BWatch,
    monkeypatch: pytest.MonkeyPatch,
    dead: bool,
) -> None:
    watch.cfg = config_mock(subscription_config(panel_alert_cooldown=60))
    panel = watch.sub.panels[0]
    panel.dead = dead
    statuses = AsyncMock(return_value=[None])
    monkeypatch.setattr(watch.sub.panel_svc, "statuses", statuses)
    alert = AsyncMock()
    watch.alert = alert
    now = 100.0
    monkeypatch.setattr("bwatch.panels.time", SimpleNamespace(monotonic=lambda: now))

    for elapsed in (0, 1, 59, 60, 61, 62):
        now = 100.0 + elapsed
        await watch.panel_health_check()

    assert alert.await_count == 2
    assert watch._panel_alerts == {panel.name: 161.0}
    if dead:
        statuses.assert_not_awaited()
        assert "Unavailable" in alert.call_args.args[0]
    else:
        assert "Unknown" in alert.call_args.args[0]
        statuses.assert_awaited_with([panel], work="background")


@pytest.mark.asyncio
async def test_unknown_status_does_not_clear_existing_metric_alert(
    watch: BWatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = AsyncMock(return_value=[panel_status(cpu=95)])
    monkeypatch.setattr(watch.sub.panel_svc, "statuses", statuses)
    monkeypatch.setattr("bwatch.panels.time", SimpleNamespace(monotonic=lambda: 100.0))
    alert = AsyncMock()
    watch.alert = alert

    await watch.panel_health_check()
    statuses.return_value = [None]
    await watch.panel_health_check()
    assert watch._panel_alerts == {watch.sub.panels[0].name: 100.0}
    statuses.return_value = [panel_status(cpu=95)]
    await watch.panel_health_check()
    alert.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("mark_dead", [False, True])
async def test_panel_status_read_failure_is_alerted(
    watch: BWatch,
    monkeypatch: pytest.MonkeyPatch,
    mark_dead: bool,
) -> None:
    panel = watch.sub.panels[0]

    async def unreadable(_url: str) -> None:
        panel.dead = mark_dead
        raise ValueError("unreadable status response")

    get = AsyncMock(side_effect=unreadable)
    monkeypatch.setattr(panel, "get", get)
    monkeypatch.setattr("bwatch.panels.time", SimpleNamespace(monotonic=lambda: 100.0))
    alert = AsyncMock()
    watch.alert = alert

    # Exercise the real status service's exception -> None contract, including
    # a session that becomes dead during the read rather than before it.
    await watch.panel_health_check()
    alert.assert_awaited_once()
    assert ("Unavailable" if mark_dead else "Unknown") in alert.call_args.args[0]
    assert watch._panel_alerts == {panel.name: 100.0}
    get.assert_awaited_once_with("panel/api/server/status")
    await watch.panel_health_check()
    assert get.await_count == (1 if mark_dead else 2)
    alert.assert_awaited_once()


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
    sub = await make_subscription(db, tmp_path)
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
        await close_subscription(sub)
        await db.close()
