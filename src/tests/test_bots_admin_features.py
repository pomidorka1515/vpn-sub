from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bot_helpers import subscription_mock, telegram_mock
from telebot import types

from bots.telegram.admin.leaderboard import AdminLeaderboardMixin
from bots.telegram.admin.panels import AdminPanelsMixin
from bots.telegram.admin.traffic import AdminTrafficMixin
from custom_types import (
    AppStats,
    DiskStats,
    MemoryStats,
    NetIOStats,
    NetTrafficStats,
    PublicIPStats,
    ServerMetricsObj,
    ServerMetricsResponse,
    SwapStats,
    UserInfo,
    UserInfoBandwidth,
    UserInfoBandwidthTotal,
    XrayStats,
)
from errors import AppError, NotFoundError
from session.client import XUiSession


def _message(text: str | None, chat_id: int = 7) -> types.Message:
    return cast(
        types.Message,
        SimpleNamespace(text=text, chat=SimpleNamespace(id=chat_id), message_id=4),
    )


def _info() -> UserInfo:
    return UserInfo(
        _="test",
        token="t" * 40,
        link="https://example.test/sub",
        displayname="Alice",
        uuid="01234567-89ab-cdef-0123-456789abcdef",
        fingerprint="chrome",
        enabled=True,
        wl_enabled=True,
        time=0,
        online=False,
        bandwidth=UserInfoBandwidth(
            total=UserInfoBandwidthTotal(upload=1_000_000_000, download=2_000_000_000, total=3),
            wl_total=UserInfoBandwidthTotal(upload=0, download=0, total=0),
            monthly=500_000_000,
            wl_monthly=0,
            limit=1,
            wl_limit=0,
        ),
    )


def _metrics(*, running: bool = True, ipv6: str = "") -> ServerMetricsResponse:
    return ServerMetricsResponse(
        success=True,
        msg="",
        obj=ServerMetricsObj(
            cpu=12.2,
            cpuCores=2,
            logicalPro=4,
            cpuSpeedMhz=2400.4,
            mem=MemoryStats(current=2 * 1024**3, total=4 * 1024**3),
            swap=SwapStats(current=0, total=1024**3),
            disk=DiskStats(current=10 * 1024**3, total=20 * 1024**3),
            xray=XrayStats(
                state="running" if running else "stopped",
                errorMsg="" if running else "crashed",
                version="1.8.0",
            ),
            uptime=90,
            loads=[0.1, 0.2, 0.3],
            tcpCount=3,
            udpCount=1,
            netIO=NetIOStats(up=2 * 1024**2, down=4 * 1024**2),
            netTraffic=NetTrafficStats(sent=1024**3, recv=2 * 1024**3),
            publicIP=PublicIPStats(ipv4="1.2.3.4", ipv6=ipv6),
            appStats=AppStats(threads=5, mem=8 * 1024**2, uptime=30),
        ),
    )


@pytest.fixture
def traffic() -> tuple[AdminTrafficMixin, MagicMock, MagicMock]:
    mixin = AdminTrafficMixin.__new__(AdminTrafficMixin)
    telegram = telegram_mock()
    subscription = subscription_mock()
    setattr(mixin, "_expect", MagicMock())
    setattr(mixin, "_clear_steps", MagicMock())
    mixin.bot = telegram
    mixin.sub = subscription
    mixin.log = MagicMock()
    lang_cfg = MagicMock()
    lang_cfg.view.return_value = {
        "description": {},
        "publicbot": {},
        "web": {},
        "chart": {"ru": {"bandwidth": "bw"}},
    }
    mixin.lang_cfg = lang_cfg
    mixin.get_main_menu = MagicMock(return_value="menu")  # type: ignore[method-assign]
    mixin._send_message = AsyncMock()  # type: ignore[method-assign]
    telegram.send_message.return_value = _message("next")
    return mixin, telegram, subscription


@pytest.mark.asyncio
async def test_chart_username_and_days_validation(
    traffic: tuple[AdminTrafficMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = traffic
    await mixin._step_chart_username(_message("/start"))
    subscription.user_svc.get_user_state.assert_not_called()

    subscription.user_svc.get_user_state.side_effect = NotFoundError("missing")
    await mixin._step_chart_username(_message("alice"))
    assert "missing" in telegram.send_message.call_args.args[1]

    subscription.user_svc.get_user_state.side_effect = None
    await mixin._step_chart_username(_message(" alice "))
    assert mixin._step_chart_days in cast(MagicMock, mixin._expect).call_args.args

    await mixin._step_chart_days(_message("/x"), "alice")
    await mixin._step_chart_days(_message("0"), "alice")
    await mixin._step_chart_days(_message("91"), "alice")
    await mixin._step_chart_days(_message("days"), "alice")
    assert "1 до 90" in telegram.send_message.call_args.args[1]

    with patch.object(AdminTrafficMixin, "_render_chart") as render:
        await mixin._step_chart_days(_message("14"), "alice")
    render.assert_awaited_once_with(username="alice", days=14, chat_id=7)


@pytest.mark.asyncio
async def test_render_chart_sends_photo_text_or_error(
    traffic: tuple[AdminTrafficMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = traffic
    subscription.bandwidth_svc.get_bw_history.return_value = ["snap"]
    subscription.business_svc.get_info.return_value = _info()
    with patch("bots.telegram.admin.traffic.bandwidth_chart", return_value=b"png") as chart:
        await mixin._render_chart(username="alice", days=7, chat_id=7)
    chart.assert_called_once()
    telegram.send_photo.assert_called_once()
    caption = telegram.send_photo.call_args.kwargs["caption"]
    assert "1.00 GB" in caption
    assert "50%" in caption
    assert "Безлимит" in caption

    with patch("bots.telegram.admin.traffic.bandwidth_chart", return_value=None):
        await mixin._render_chart(username="alice", days=7, chat_id=7)
    assert "Нет данных" in telegram.send_message.call_args.args[1]

    subscription.business_svc.get_info.side_effect = AppError("denied")
    await mixin._render_chart(username="alice", days=7, chat_id=7)
    assert "denied" in cast(MagicMock, mixin._send_message).call_args.args[1]

    subscription.business_svc.get_info.side_effect = RuntimeError("boom")
    await mixin._render_chart(username="alice", days=7, chat_id=7)
    assert "Внутренняя ошибка" in cast(MagicMock, mixin._send_message).call_args.args[1]
    cast(MagicMock, mixin.log).exception.assert_called()


@pytest.fixture
def leaderboard() -> tuple[AdminLeaderboardMixin, MagicMock, MagicMock]:
    mixin = AdminLeaderboardMixin.__new__(AdminLeaderboardMixin)
    telegram = telegram_mock()
    subscription = subscription_mock()
    setattr(mixin, "_expect", MagicMock())
    setattr(mixin, "_clear_steps", MagicMock())
    mixin.bot = telegram
    mixin.sub = subscription
    lang_cfg = MagicMock()
    lang_cfg.view.return_value = {
        "description": {},
        "publicbot": {},
        "web": {},
        "chart": {"ru": {"leaderboard": "lb"}},
    }
    mixin.lang_cfg = lang_cfg
    mixin.get_main_menu = MagicMock(return_value="menu")  # type: ignore[method-assign]
    mixin._pending_leaderboard = {}
    mixin._cb_leaderboard_window = AsyncMock()  # type: ignore[method-assign]
    telegram.send_message.return_value = _message("next")
    return mixin, telegram, subscription


@pytest.mark.asyncio
async def test_leaderboard_prompts_and_window(
    leaderboard: tuple[AdminLeaderboardMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = leaderboard
    await mixin._cb_leaderboard_type(7)
    await mixin._cb_leaderboard_order(7)
    cast(MagicMock, mixin._cb_leaderboard_window).assert_not_called()
    cast(MagicMock, mixin._expect).side_effect = None
    mixin._cb_leaderboard_window = AdminLeaderboardMixin._cb_leaderboard_window.__get__(mixin)  # type: ignore[method-assign]
    await mixin._cb_leaderboard_window(7)
    registered = cast(MagicMock, mixin._expect).call_args.args[1]
    assert (
        getattr(registered, "__func__", registered)
        is AdminLeaderboardMixin._step_leaderboard_window
    )

    await mixin._step_leaderboard_window(_message(None))
    await mixin._step_leaderboard_window(_message("-1"))
    await mixin._step_leaderboard_window(_message("nope"))
    assert "неотрицательное" in telegram.send_message.call_args.args[1]

    await mixin._step_leaderboard_window(_message("5"))
    assert "истекла" in telegram.send_message.call_args.args[1]

    mixin._pending_leaderboard[7] = {"type": "monthly", "order": "asc"}
    with patch.object(AdminLeaderboardMixin, "_handle_leaderboard_result") as result:
        await mixin._step_leaderboard_window(_message("3"))
    result.assert_called_once_with(7, "monthly", "asc", 3)
    assert 7 not in mixin._pending_leaderboard
    subscription.leaderboard_svc.leaderboard.assert_not_called()


@pytest.mark.asyncio
async def test_leaderboard_result_renders_or_reports_empty(
    leaderboard: tuple[AdminLeaderboardMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = leaderboard
    subscription.leaderboard_svc.leaderboard.return_value = {"alice": 10, "bob": 20}
    with patch("bots.telegram.admin.leaderboard.leaderboard_chart", return_value=None):
        await mixin._handle_leaderboard_result(7, "total", "desc", 0)
    telegram.send_message.assert_called_with(7, "Нет данных!")

    with patch("bots.telegram.admin.leaderboard.leaderboard_chart", return_value=b"png"):
        await mixin._handle_leaderboard_result(7, "total", "desc", 0)
    caption = telegram.send_photo.call_args.args[2]
    assert caption.index("bob") < caption.index("alice")
    assert "🥇" in caption

    long_name = "ю" * 600
    subscription.leaderboard_svc.leaderboard.return_value = {long_name: 5}
    with patch("bots.telegram.admin.leaderboard.leaderboard_chart", return_value=b"png"):
        await mixin._handle_leaderboard_result(7, "monthly", "asc", 2)
    caption = telegram.send_photo.call_args.args[2]
    assert "..." in caption
    assert len(caption.encode("utf-8")) <= 1024 + len(b"...")


@pytest.fixture
def panels() -> tuple[AdminPanelsMixin, MagicMock, MagicMock]:
    mixin = AdminPanelsMixin.__new__(AdminPanelsMixin)
    telegram = telegram_mock()
    subscription = subscription_mock()
    setattr(mixin, "_expect", MagicMock())
    setattr(mixin, "_clear_steps", MagicMock())
    mixin.bot = telegram
    mixin.sub = subscription
    mixin.log = MagicMock()
    mixin.get_main_menu = MagicMock(return_value="menu")  # type: ignore[method-assign]
    mixin._send_message = AsyncMock()  # type: ignore[method-assign]
    mixin._delete_message = AsyncMock()  # type: ignore[method-assign]
    telegram.send_message.return_value = SimpleNamespace(message_id=9)
    return mixin, telegram, subscription


@pytest.mark.asyncio
async def test_panel_status_renders_running_and_unknown(
    panels: tuple[AdminPanelsMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = panels
    panel = SimpleNamespace(name="edge")
    subscription.panel_svc.getstatus.return_value = None
    await mixin._cb_panel_info(7, cast(XUiSession, panel), True)
    assert "неизвестен" in cast(MagicMock, mixin._send_message).call_args.args[1]

    subscription.panel_svc.getstatus.return_value = _metrics(ipv6="::1")
    await mixin._cb_panel_info(7, cast(XUiSession, panel), True)
    text = telegram.send_message.call_args.args[1]
    assert "🟢 Работает" in text
    assert "::1" in text
    assert telegram.send_message.call_args.kwargs["reply_markup"] == "menu"

    subscription.panel_svc.getstatus.return_value = _metrics(running=False)
    await mixin._cb_panel_info(7, cast(XUiSession, panel), False)
    text = telegram.send_message.call_args.args[1]
    assert "🔴 crashed" in text
    assert "Отключен" in text
    assert telegram.send_message.call_args.kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_all_panels_status_continues_after_failure(
    panels: tuple[AdminPanelsMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = panels
    first = SimpleNamespace(name="one")
    second = SimpleNamespace(name="two")
    subscription.panels = [first, second]

    def status(chat_id: int, panel: object, last: bool) -> None:
        del chat_id, last
        if cast(XUiSession, panel).name == "one":
            raise RuntimeError("down")

    subscription.panel_svc.statuses.return_value = [None, None]
    with patch.object(AdminPanelsMixin, "_cb_panel_info", side_effect=status):
        await mixin._cb_all_panels_status(7)
    assert "Внутренняя ошибка" in cast(MagicMock, mixin._send_message).call_args.args[1]
    cast(MagicMock, mixin._delete_message).assert_called_once_with(7, 9)
    telegram.send_message.assert_called_once()
