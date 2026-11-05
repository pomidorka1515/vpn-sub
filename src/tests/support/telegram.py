from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from core.services.bandwidth import BandwidthService
from core.services.code import BusinessCodeService, CodeService
from core.services.leaderboard import LeaderboardService
from core.services.panel import PanelService
from core.services.password import PasswordService
from core.services.telegram import TelegramService
from core.services.user import BusinessUserService, CommonUserService


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
        ("user_svc", CommonUserService),
        ("business_svc", BusinessUserService),
        ("code_svc", CodeService),
        ("business_code_svc", BusinessCodeService),
        ("telegram_svc", TelegramService),
        ("bandwidth_svc", BandwidthService),
        ("panel_svc", PanelService),
        ("password_svc", PasswordService),
        ("leaderboard_svc", LeaderboardService),
    ):
        setattr(sub, name, MagicMock(spec=service))

    async def render(function: object, *args: object, **kwargs: object) -> object:
        from collections.abc import Callable
        from typing import cast

        return cast(Callable[..., object], function)(*args, **kwargs)

    sub.res.offloads.rendering.run = AsyncMock(side_effect=render)
    return sub
