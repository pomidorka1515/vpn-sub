from __future__ import annotations

import os
from typing import TYPE_CHECKING

import discord
from app_runtime.tasks import TaskRegistry
from config import Config, DiscordLangConfig
from config.store import AsyncConfig
from discord_bot.admin import AdminBot
from discord_bot.adminapi import AdminApiClient
from discord_bot.host import SharedDiscordClient
from discord_bot.public import PublicBot
from discord_bot.sessions import DatabaseSessionStore
from discord_bot.transport import LocalTransport
from discord_bot.webapi import WebApiClient
from paths import bundled_root
from threadutil import named_to_thread

if TYPE_CHECKING:
    import asyncio
    from pathlib import Path

    from quart import Quart

    from app_runtime.offload import BoundedOffload
    from config import AppConfig, DiscordConfig
    from core.common import Snapshot
    from db import Database


class DiscordSettings:
    def __init__(self, cfg: Snapshot[AppConfig]) -> None:
        self.cfg = cfg

    def view(self) -> DiscordConfig:
        section = self.cfg.view().get("discord")
        if section is None:
            raise RuntimeError("Discord configuration missing")
        return {"public": {"token": section["token"]}, "private": {"whitelist": section["whitelist"]}}


class DiscordApplication:
    def __init__(self, app: Quart, cfg: Snapshot[AppConfig], data: Path, db: Database, *, files: BoundedOffload, rendering: BoundedOffload) -> None:
        self.cfg = cfg
        self.data = data
        self.app = app
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
        transport = LocalTransport(self.app)
        public = PublicBot(DiscordSettings(self.cfg), self.lang, WebApiClient("http://local", conf["uri"], transport=transport), self.sessions, client=self.client, tree=self.client.tree, rendering=self.rendering)
        admin = AdminBot(DiscordSettings(self.cfg), self.lang, AdminApiClient("http://local", conf["uri"], conf.get("api_uri", ""), conf["api_token"], transport=transport), client=self.client, tree=self.client.tree, rendering=self.rendering)
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
