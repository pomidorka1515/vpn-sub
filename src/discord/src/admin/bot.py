from __future__ import annotations

from loggers import Logger

__all__ = ["AdminBot"]


class AdminBot:
    def __init__(self) -> None:
        self.log = Logger(type(self).__name__)

    async def start(self) -> None:
        self.log.info("admin discord bot is a stub; not starting")

    async def stop(self) -> None:
        return
