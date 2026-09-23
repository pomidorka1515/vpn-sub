from __future__ import annotations

from admin.bot import AdminBot
from discord_helpers import run


def test_start_does_not_open_network_client() -> None:
    bot = AdminBot()
    assert not hasattr(bot, "bot")
    run(bot.start())
    assert not hasattr(bot, "bot")
    run(bot.stop())
