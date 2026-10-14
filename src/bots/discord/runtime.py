from __future__ import annotations

import os
from typing import TYPE_CHECKING

import discord
from app_runtime.tasks import TaskRegistry
from bots.discord.admin import AdminBot
from bots.discord.host import SharedDiscordClient
from bots.discord.public import PublicBot
from bots.discord.sessions import DatabaseSessionStore
from config import Config, DiscordLangConfig
from config.store import AsyncConfig
from paths import bundled_root
from threadutil import named_to_thread

if TYPE_CHECKING:
    import asyncio
    from pathlib import Path

    from app_runtime.offload import BoundedOffload
    from app_runtime.rate_limit import AsyncRateLimiter
    from config import AppConfig
    from core import Subscription
    from core.common import Snapshot
    from db import Database


class DiscordApplication:
    def __init__(
        self,
        sub: Subscription,
        cfg: Snapshot[AppConfig],
        data: Path,
        db: Database,
        *,
        files: BoundedOffload,
        rendering: BoundedOffload,
        limiter: AsyncRateLimiter,
    ) -> None:
        self.cfg = cfg
        self.data = data
        self.sub = sub
        self.limiter = limiter
        self.db = db
        self.files = files
        self.rendering = rendering
        self.client = SharedDiscordClient(intents=discord.Intents.default())
        self.runner: asyncio.Task[None] | None = None
        self.lang: AsyncConfig[DiscordLangConfig] | None = None
        self.sessions: DatabaseSessionStore | None = None
        self._started = False
        self.tasks = TaskRegistry()

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        root = bundled_root()
        language = root / "discord.lang.jsonc"
        if not await named_to_thread("files:discord-language-exists", language.is_file):
            language = root / "src/discord/lang.jsonc"
        self.lang = AsyncConfig[DiscordLangConfig](
            lambda: Config[DiscordLangConfig](
                path=os.getenv("PATH_DISCORD_LANG", str(language)),
                read_only=True,
                read_only_jsonc=True,
                start_backup=False,
            ),
            files=self.files,
        )
        await self.lang.start()
        self.sessions = DatabaseSessionStore(self.db)
        await self.sessions.start(self.data / "discord-sessions.json")
        conf = self.cfg.view()
        public = PublicBot(
            self.cfg,
            self.lang,
            self.sub,
            self.sessions,
            client=self.client,
            tree=self.client.tree,
            rendering=self.rendering,
            limiter=self.limiter,
        )
        admin = AdminBot(
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
        if self.lang is not None:
            await self.lang.close()
