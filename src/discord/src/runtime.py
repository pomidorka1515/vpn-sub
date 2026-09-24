from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import discord

from admin import AdminBot
from adminapi import AdminApiClient
from config import Config, ConfigLike, LinesConfig
from host import SharedDiscordClient
from loggers import Logger
from public import PublicBot
from sessions import SessionStore
from webapi import WebApiClient

__all__ = [
    "DiscordApplication",
    "DiscordPaths",
    "create_application",
    "main",
]

log = Logger("discord")


@dataclass(frozen=True, slots=True, kw_only=True)
class DiscordPaths:
    data: Path
    config: Path
    language: Path
    log: Path
    sessions: Path
    backups: Path
    http_url: str
    uri: str
    api_uri: str

    @classmethod
    def from_env(cls) -> DiscordPaths:
        root = Path(__file__).resolve().parents[3]
        data = Path(os.getenv("DIR_DATA", root / "data"))
        return cls(
            data=data,
            config=Path(os.getenv("PATH_DISCORD_CONFIG", data / "discord.json")),
            language=Path(os.getenv("PATH_DISCORD_LANG", root / "src" / "discord" / "lang.jsonc")),
            log=Path(os.getenv("PATH_LOG", data / "log.jsonl")),
            sessions=Path(os.getenv("PATH_DISCORD_SESSIONS", data / "discord-sessions.json")),
            backups=data / "backup",
            http_url=os.getenv("SUB_HTTP_URL", "http://127.0.0.1:5550"),
            uri=os.getenv("SUB_URI", "sub"),
            api_uri=os.getenv("SUB_API_URI", "privapi"),
        )


@dataclass(slots=True, kw_only=True)
class DiscordApplication:
    cfg: Config
    lang_cfg: Config
    log_cfg: LinesConfig
    http: WebApiClient
    admin_http: AdminApiClient
    sessions: SessionStore
    client: SharedDiscordClient
    public_bot: PublicBot
    admin_bot: AdminBot
    _token: str
    _stop: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _runner: asyncio.Task[None] | None = field(default=None, init=False)

    async def start(self) -> None:
        await self.http.start()
        await self.admin_http.start()
        await self.admin_bot.start()
        await self.public_bot.start()
        await self.client.login(self._token)
        self._runner = asyncio.create_task(self.client.connect(reconnect=True), name="discord")

    async def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        runner = self._runner
        if self.client.is_closed() is False:
            await self.client.close()
        if runner is not None:
            try:
                await runner
            except Exception:
                log.error("discord runner failed", exc_info=True)
        await self.public_bot.stop()
        await self.admin_bot.stop()
        await self.http.close()
        await self.admin_http.close()
        for resource in (self.sessions, self.cfg, self.lang_cfg, self.log_cfg):
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    log.error("resource cleanup failed", exc_info=True)


def create_application(paths: DiscordPaths | None = None) -> DiscordApplication:
    paths = paths or DiscordPaths.from_env()
    paths.data.mkdir(parents=True, exist_ok=True)
    paths.backups.mkdir(parents=True, exist_ok=True)

    cfg = Config(
        path=paths.config,
        indent=4,
        read_only=False,
        strict_schema=True,
        sync_mode="data",
        isolate_commits=True,
        backup_dir=paths.backups,
    )
    lang_cfg = Config(
        path=paths.language,
        indent=4,
        read_only=True,
        read_only_jsonc=True,
        strict_schema=False,
    )
    log_cfg = LinesConfig(path=paths.log, sync_mode="data", backup_dir=paths.backups)
    log.set_jsonl_handler(log_cfg)

    with log.loading():
        token = cfg["public"]["token"]
        if not isinstance(token, str) or not token:
            raise RuntimeError("public discord bot token not found in discord.json")
        private = cfg["private"]
        api_token = private["api_token"]
        if not isinstance(api_token, str) or not api_token:
            raise RuntimeError("private discord api_token not found in discord.json")
        http = WebApiClient(base=paths.http_url, uri=paths.uri)
        admin_http = AdminApiClient(
            base=paths.http_url,
            uri=paths.uri,
            api_uri=paths.api_uri,
            token=api_token,
        )
        sessions = SessionStore(paths.sessions)
        client = SharedDiscordClient(intents=discord.Intents.default())
        public_bot = PublicBot(
            cfg=cast(ConfigLike, cfg),
            lang_cfg=cast(ConfigLike, lang_cfg),
            http=http,
            sessions=sessions,
            client=client,
            tree=client.tree,
        )
        admin_bot = AdminBot(
            cfg=cast(ConfigLike, cfg),
            lang_cfg=cast(ConfigLike, lang_cfg),
            http=admin_http,
            client=client,
            tree=client.tree,
        )
        client.attach(public_bot, admin_bot)
        return DiscordApplication(
            cfg=cfg,
            lang_cfg=lang_cfg,
            log_cfg=log_cfg,
            http=http,
            admin_http=admin_http,
            sessions=sessions,
            client=client,
            public_bot=public_bot,
            admin_bot=admin_bot,
            _token=token,
        )


async def _run() -> None:
    runtime = create_application()
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()

    def _request_stop(*_args: Any) -> None:
        stopping.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda _signum, _frame: _request_stop())

    await runtime.start()
    try:
        runner = runtime._runner
        waiters: list[asyncio.Future[Any]] = [asyncio.ensure_future(stopping.wait())]
        if isinstance(runner, asyncio.Task):
            waiters.append(runner)
        done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for waiter in pending:
            waiter.cancel()
        if isinstance(runner, asyncio.Task) and runner in done:
            try:
                await runner
            except Exception:
                log.error("discord runner failed", exc_info=True)
    finally:
        await runtime.stop()


def main() -> None:
    asyncio.run(_run())
