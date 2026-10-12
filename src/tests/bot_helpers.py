from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from core.services.async_bandwidth import AsyncBandwidthService
from core.services.async_code import AsyncBusinessCodeService, AsyncCodeService
from core.services.async_leaderboard import AsyncLeaderboardService
from core.services.async_panel import AsyncPanelService
from core.services.async_password import AsyncPasswordService
from core.services.async_telegram import AsyncTelegramService
from core.services.async_user import AsyncBusinessUserService, AsyncCommonUserService


def telegram_mock() -> MagicMock:
    bot = MagicMock()
    for method in (
        "send_message",
        "send_photo",
        "edit_message_text",
        "delete_message",
        "answer_callback_query",
        "get_updates",
        "process_new_updates",
    ):
        setattr(bot, method, AsyncMock())
    return bot


def subscription_mock() -> MagicMock:
    sub = MagicMock()
    for name, service in (
        ("user_svc", AsyncCommonUserService),
        ("business_svc", AsyncBusinessUserService),
        ("code_svc", AsyncCodeService),
        ("business_code_svc", AsyncBusinessCodeService),
        ("telegram_svc", AsyncTelegramService),
        ("bandwidth_svc", AsyncBandwidthService),
        ("panel_svc", AsyncPanelService),
        ("password_svc", AsyncPasswordService),
        ("leaderboard_svc", AsyncLeaderboardService),
    ):
        setattr(sub, name, MagicMock(spec=service))

    async def render(function: object, *args: object, **kwargs: object) -> object:
        from collections.abc import Callable
        from typing import cast

        return cast(Callable[..., object], function)(*args, **kwargs)

    sub.res.offloads.rendering.run = AsyncMock(side_effect=render)
    return sub
