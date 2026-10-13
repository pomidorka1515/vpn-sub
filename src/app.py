from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING

from api import AsyncApi, AsyncWebApi
from api.application import create_api_application
from api.decorators.rate_limit import AsyncRateLimiter
from api.subscription import register_subscription_route
from app_runtime.composition import PrimaryLock, build_panels
from app_runtime.interfaces import OwnedResource
from app_runtime.lifecycle import Runtime
from app_runtime.offload import OffloadServices
from app_runtime.settings import AppOptions, AppPaths, PanelTransportFactory, verbose_overrides
from app_runtime.tasks import MutationRegistry
from bots import AsyncAdminBot, AsyncPublicBot
from bots.logging import AsyncBotLogging
from bots.polling import AsyncTelegramSession
from bwatch.watch import BWatch
from config import AppConfig, Config, LangConfig, LinesConfig
from config.store import AsyncConfig, AsyncLinesConfig
from core import Subscription
from core.coordination import MutationCoordinator
from core.services.audit import AuditService
from db import Database
from discord_bot.runtime import DiscordApplication
from notify.notifier import Notifier
from paths import bundled_root, compiled

if TYPE_CHECKING:
    from quart import Quart

    from loggers import Logger
    from session.client import XUiSession

__all__ = [
    "AppOptions",
    "AppPaths",
    "Application",
    "PanelTransportFactory",
    "create_application",
    "verbose_overrides",
]


@dataclass(slots=True)
class ApplicationDependencies:
    app: Quart
    cfg: AsyncConfig[AppConfig]
    lang_cfg: AsyncConfig[LangConfig]
    audit_cfg: AsyncLinesConfig
    db: Database
    panels: list[XUiSession]
    whitelist_panel: XUiSession | None
    subscription: Subscription
    limiter: AsyncRateLimiter
    watcher: BWatch
    admin_bot: AsyncAdminBot | None = None
    public_bot: AsyncPublicBot | None = None
    discord_bot: DiscordApplication | None = None


class Application(Runtime[ApplicationDependencies]):
    @property
    def app(self) -> Quart:
        return self.dependencies.app


async def create_application(
    paths: AppPaths | None = None,
    options: AppOptions | None = None,
    *,
    app: Quart | None = None,
) -> Application:
    """Compose Quart against runtime-owned async dependencies, unwinding failures."""
    paths = paths or AppPaths.from_env()
    options = options or AppOptions()
    offloads = OffloadServices()
    opened: list[OwnedResource] = []

    async def open_resource(resource: OwnedResource, log: Logger | None = None) -> None:
        opened.append(resource)
        with log.loading() if log is not None else nullcontext():
            await resource.resource.start()

    try:
        await open_resource(OwnedResource("primary lock", PrimaryLock(paths.primary_lock)))
        cfg = AsyncConfig[AppConfig](
            lambda: Config[AppConfig](
                path=paths.config,
                indent=4,
                strict_schema=True,
                schema_path=bundled_root() / "config.schema.json" if compiled() else None,
                sync_mode="data",
                isolate_commits=True,
                backup_dir=paths.backups,
                lockfile_path=paths.primary_lock.parent,
                start_backup=False,
            ),
            files=offloads.files,
        )
        lang = AsyncConfig[LangConfig](
            lambda: Config[LangConfig](
                path=paths.language,
                read_only=True,
                read_only_jsonc=True,
                lockfile_path=paths.primary_lock.parent,
            ),
            files=offloads.files,
        )
        audit = AsyncLinesConfig(
            lambda: LinesConfig(
                path=paths.audit,
                sync_mode="data",
                backup_dir=paths.backups,
                lockfile_path=paths.primary_lock.parent,
                start_backup=False,
            ),
            files=offloads.files,
        )
        for name, store in (("config", cfg), ("language", lang), ("audit", audit)):
            await open_resource(OwnedResource(name, store))
        db = Database(
            path=paths.database,
            backup_dir=paths.backups,
            start_backup=options.start_background,
        )
        await open_resource(OwnedResource("database", db), db.log)
        limiter = options.limiter or AsyncRateLimiter.from_config(cfg)
        await open_resource(OwnedResource("Redis", limiter))
        overrides = verbose_overrides()
        panels, whitelist = build_panels(cfg, paths, options, verbose="panels" in overrides)
        for panel in [*panels, *([whitelist] if whitelist else [])]:
            await open_resource(OwnedResource(f"panel:{panel.name}", panel), panel.log)
        mutations = MutationRegistry()
        sub = Subscription(
            cfg=cfg,
            lang_cfg=lang,
            db=db,
            panels=panels,
            whitelist_panel=whitelist,
            offloads=offloads,
            coordinator=MutationCoordinator(mutations),
            audit_svc=AuditService(audit),
            verbose="core" in overrides,
        )
        await open_resource(OwnedResource("subscription", sub), sub.res.log)
        app = app or create_api_application(
            proxy_hops=options.proxy_hops,
            require_proxy=options.require_proxy,
        )
        conf = cfg.view()
        admin_bot = (
            AsyncAdminBot(sub, lang, cfg)
            if options.start_bots and conf.get("bot", {}).get("token")
            else None
        )
        public_bot = (
            AsyncPublicBot(sub, cfg, lang)
            if options.start_bots and conf.get("publicbot", {}).get("token")
            else None
        )
        if admin_bot is not None or public_bot is not None:
            await open_resource(OwnedResource("Telegram session", AsyncTelegramSession()))
        notifier = (
            Notifier(
                texts=public_bot.TEXTS,
                send=public_bot.send_notice,
                language=public_bot.get_lang,
            )
            if public_bot is not None
            else None
        )
        status = BWatch(
            cfg,
            db,
            sub,
            notifier=notifier,
            alert=admin_bot.msg if admin_bot is not None else None,
            scheduled=options.start_background,
            verbose="bwatch" in overrides,
        )
        if admin_bot is not None:
            await open_resource(OwnedResource("Telegram log delivery", AsyncBotLogging(admin_bot)))
        await open_resource(OwnedResource("watcher", status, quiesce=status.close), status.log)
        for name, bot in (("admin bot", admin_bot), ("public bot", public_bot)):
            if bot is not None:
                await open_resource(OwnedResource(name, bot, quiesce=bot.close))
        AsyncApi(app=app, cfg=cfg, audit_cfg=audit, sub=sub, bw=status, limiter=limiter)
        AsyncWebApi(app=app, cfg=cfg, sub=sub, bw=status, limiter=limiter)
        register_subscription_route(app, sub)
        discord_bot = (
            DiscordApplication(app, cfg, paths.data, db)
            if options.start_bots and conf.get("discord", {}).get("token")
            else None
        )
        if discord_bot is not None:
            await open_resource(OwnedResource("Discord bot", discord_bot, quiesce=discord_bot.close))
        runtime = Application(
            ApplicationDependencies(
                app,
                cfg,
                lang,
                audit,
                db,
                panels,
                whitelist,
                sub,
                limiter,
                status,
                admin_bot,
                public_bot,
                discord_bot,
            ),
            resources=tuple(opened),
            offloads=offloads,
            mutations=mutations,
        )
        await runtime.start()
        app.extensions["runtime"] = runtime
        return runtime
    except Exception, asyncio.CancelledError:
        for resource in reversed(opened):
            await resource.resource.close()
        await offloads.close()
        raise
