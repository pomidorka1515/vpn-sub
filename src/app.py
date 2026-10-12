from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from api import AsyncApi, AsyncWebApi
from api.application import create_api_application
from api.decorators.rate_limit import AsyncRateLimiter
from api.subscription import register_subscription_route
from app_runtime.composition import PrimaryLock, SnapshotStatus, build_panels
from app_runtime.interfaces import OwnedResource
from app_runtime.lifecycle import Runtime
from app_runtime.offload import OffloadServices
from app_runtime.settings import AppOptions, AppPaths, PanelTransportFactory, verbose_overrides
from app_runtime.tasks import MutationRegistry
from config import AppConfig, Config, LangConfig, LinesConfig
from config.async_store import AsyncConfig, AsyncLinesConfig
from core import AsyncSubscription
from core.coordination import MutationCoordinator
from core.services.async_audit import AsyncAuditService
from db import AsyncDatabase
from paths import bundled_root, compiled

if TYPE_CHECKING:
    from quart import Quart

    from session.async_client import AsyncXUiSession

__all__ = ["AppOptions", "AppPaths", "Application", "PanelTransportFactory", "create_application", "verbose_overrides"]


@dataclass(slots=True)
class ApplicationDependencies:
    app: Quart
    cfg: AsyncConfig[AppConfig]
    lang_cfg: AsyncConfig[LangConfig]
    audit_cfg: AsyncLinesConfig
    db: AsyncDatabase
    panels: list[AsyncXUiSession]
    whitelist_panel: AsyncXUiSession | None
    subscription: AsyncSubscription
    limiter: AsyncRateLimiter


class Application(Runtime[ApplicationDependencies]):
    @property
    def app(self) -> Quart:
        return self.dependencies.app


async def create_application(paths: AppPaths | None = None, options: AppOptions | None = None, *, app: Quart | None = None) -> Application:
    """Compose Quart against runtime-owned async dependencies, unwinding failures."""
    paths = paths or AppPaths.from_env()
    options = options or AppOptions()
    offloads = OffloadServices()
    opened: list[OwnedResource] = []

    async def open_resource(resource: OwnedResource) -> None:
        opened.append(resource)
        await resource.resource.start()

    try:
        await open_resource(OwnedResource("primary lock", PrimaryLock(paths.primary_lock)))
        cfg = AsyncConfig[AppConfig](lambda: Config[AppConfig](path=paths.config, indent=4, strict_schema=True, schema_path=bundled_root() / "config.schema.json" if compiled() else None, sync_mode="data", isolate_commits=True, backup_dir=paths.backups, lockfile_path=paths.primary_lock.parent, start_backup=False), files=offloads.files)
        lang = AsyncConfig[LangConfig](lambda: Config[LangConfig](path=paths.language, read_only=True, read_only_jsonc=True, lockfile_path=paths.primary_lock.parent), files=offloads.files)
        audit = AsyncLinesConfig(lambda: LinesConfig(path=paths.audit, sync_mode="data", backup_dir=paths.backups, lockfile_path=paths.primary_lock.parent, start_backup=False), files=offloads.files)
        for name, store in (("config", cfg), ("language", lang), ("audit", audit)):
            await open_resource(OwnedResource(name, store))
        db = AsyncDatabase(path=paths.database, backup_dir=paths.backups, start_backup=options.start_background)
        await open_resource(OwnedResource("database", db))
        limiter = options.limiter or AsyncRateLimiter.from_config(cfg)
        await open_resource(OwnedResource("Redis", limiter))
        overrides = verbose_overrides()
        panels, whitelist = build_panels(cfg, paths, options, verbose="panels" in overrides)
        for panel in [*panels, *([whitelist] if whitelist else [])]:
            await open_resource(OwnedResource(f"panel:{panel.name}", panel))
        mutations = MutationRegistry()
        sub = AsyncSubscription(cfg=cfg, lang_cfg=lang, db=db, panels=panels, whitelist_panel=whitelist, offloads=offloads, coordinator=MutationCoordinator(mutations), audit_svc=AsyncAuditService(audit), verbose="core" in overrides)
        await open_resource(OwnedResource("subscription", sub))
        app = app or create_api_application(proxy_hops=options.proxy_hops, require_proxy=options.require_proxy)
        status = SnapshotStatus()
        AsyncApi(app=app, cfg=cfg, audit_cfg=audit, sub=sub, bw=status, limiter=limiter)
        AsyncWebApi(app=app, cfg=cfg, sub=sub, bw=status, limiter=limiter)
        register_subscription_route(app, sub)
        runtime = Application(ApplicationDependencies(app, cfg, lang, audit, db, panels, whitelist, sub, limiter), resources=tuple(opened), offloads=offloads, mutations=mutations)
        await runtime.start()
        app.extensions["runtime"] = runtime
        return runtime
    except (Exception, asyncio.CancelledError):
        for resource in reversed(opened):
            await resource.resource.close()
        await offloads.close()
        raise
