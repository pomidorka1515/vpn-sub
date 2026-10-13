"""Runtime-owned admin alert delivery through the async logging queue."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from loggers.delivery import AsyncTelegramLogger, LogDelivery

if TYPE_CHECKING:
    from .admin import AsyncAdminBot


class AsyncBotLogging:
    def __init__(self, bot: AsyncAdminBot) -> None:
        self.delivery = LogDelivery(bot.msg, name="logs:Telegram")
        self.handler = AsyncTelegramLogger(self.delivery)
        self.handler.setLevel(logging.WARNING)
        self.handler.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))
        self._loggers: list[logging.Logger] = []

    async def start(self) -> None:
        await self.delivery.start()
        self._loggers = [
            logger
            for logger in logging.Logger.manager.loggerDict.values()
            if isinstance(logger, logging.Logger)
        ]
        for logger in self._loggers:
            logger.addHandler(self.handler)

    async def close(self) -> None:
        for logger in self._loggers:
            logger.removeHandler(self.handler)
        self._loggers.clear()
        await self.delivery.close()
