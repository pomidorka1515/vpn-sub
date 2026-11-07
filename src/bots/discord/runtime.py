from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from app_runtime.tasks import TaskRegistry
from bots.discord.admin import DiscordAdminBot
from bots.discord.host import SharedDiscordClient
from bots.discord.public import DiscordPublicBot
from bots.discord.sessions import DatabaseSessionStore

if TYPE_CHECKING:
    import asyncio
    from pathlib import Path

    from app_runtime.offload import BoundedOffload
    from app_runtime.rate_limit import AsyncRateLimiter
    from config import AppConfig, DiscordLangConfig, LangConfig
    from core import Subscription
    from core.common import Snapshot
    from db import Database


class DiscordLanguage:
    def __init__(self, language: Snapshot[LangConfig]) -> None:
        self.language = language

    def view(self) -> DiscordLangConfig:
        language = self.language.view().get("discord")
        if language is None:
            raise ValueError("Discord language section missing from lang.jsonc")
        return language


class DiscordApplication:
    def __init__(
        self,
        sub: Subscription,
        cfg: Snapshot[AppConfig],
        language: Snapshot[LangConfig],
        data: Path,
        db: Database,
        *,
        rendering: BoundedOffload,
        limiter: AsyncRateLimiter,
    ) -> None:
        self.cfg = cfg
        self.data = data
        self.sub = sub
        self.limiter = limiter
        self.db = db
        self.lang = DiscordLanguage(language)
        self.rendering = rendering
        self.client = SharedDiscordClient(intents=discord.Intents.default())
        self.runner: asyncio.Task[None] | None = None
        self.sessions: DatabaseSessionStore | None = None
        self._started = False
        self.tasks = TaskRegistry()

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.sessions = DatabaseSessionStore(self.db)
        await self.sessions.start(self.data / "discord-sessions.json")
        conf = self.cfg.view()
        public = DiscordPublicBot(
            self.cfg,
            self.lang,
            self.sub,
            self.sessions,
            client=self.client,
            tree=self.client.tree,
            rendering=self.rendering,
            limiter=self.limiter,
        )
        admin = DiscordAdminBot(
            self.cfg,
            self.lang,
            self.sub,
            client=self.client,
            tree=self.client.tree,
            rendering=self.rendering,
            limiter=self.limiter,
        )
        self.client.attach(public, admin)
        section = conf.get("discord")
        if section is None:
            raise RuntimeError("Discord configuration missing")
        await self.client.login(section["token"])
        self.runner = self.tasks.create(lambda: self.client.connect(reconnect=True), name="discord")

    async def close(self) -> None:
        await self.client.close()
        await self.tasks.drain(cancel=True)
        self.runner = None
        if self.sessions is not None:
            await self.sessions.settle()
