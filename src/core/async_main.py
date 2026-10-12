from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from argon2 import PasswordHasher

from loggers import Logger

from .async_common import AsyncCoreResources
from .services.async_bandwidth import AsyncBandwidthService
from .services.async_code import AsyncBusinessCodeService, AsyncCodeService
from .services.async_leaderboard import AsyncLeaderboardService
from .services.async_panel import AsyncPanelService
from .services.async_password import AsyncPasswordService
from .services.async_telegram import AsyncTelegramService
from .services.async_user import AsyncBusinessUserService, AsyncCommonUserService

if TYPE_CHECKING:
    from app_runtime.offload import OffloadServices
    from config import AppConfig, LangConfig
    from db.async_database import AsyncDatabase
    from session.async_client import AsyncXUiSession

    from .async_common import Snapshot
    from .coordination import MutationCoordinator
    from .services.async_audit import AsyncAuditService


class AsyncSubscription:
    """Framework-free core; injected resources remain runtime-owned.

    Startup recovery is explicit and must precede public admission. Close
    drains mutations and cache/fanout tasks before the runtime closes I/O.
    """

    def __init__(
        self,
        *,
        cfg: Snapshot[AppConfig],
        lang_cfg: Snapshot[LangConfig],
        db: AsyncDatabase,
        panels: list[AsyncXUiSession],
        whitelist_panel: AsyncXUiSession | None,
        offloads: OffloadServices,
        coordinator: MutationCoordinator,
        audit_svc: AsyncAuditService,
        password_hasher: PasswordHasher | None = None,
        verbose: bool = False,
    ) -> None:
        self.panels = list(panels)
        if whitelist_panel is not None and whitelist_panel not in self.panels:
            self.panels.append(whitelist_panel)
        self.res = AsyncCoreResources(
            log=Logger(type(self).__name__),
            cfg=cfg,
            lang_cfg=lang_cfg,
            db=db,
            panels=self.panels,
            whitelist_panel=whitelist_panel,
            offloads=offloads,
            coordinator=coordinator,
            password_hasher=password_hasher or PasswordHasher(),
            legacy_salt=cfg.view()["salt"],
            verbose=verbose,
        )
        self.audit_svc = audit_svc
        self.panel_svc = AsyncPanelService(self.res)
        self.password_svc = AsyncPasswordService(self.res)
        self.user_svc = AsyncCommonUserService(self.res)
        self.bandwidth_svc = AsyncBandwidthService(
            self.res, panel_svc=self.panel_svc, user_svc=self.user_svc
        )
        self.business_svc = AsyncBusinessUserService(
            self.res,
            user_svc=self.user_svc,
            panel_svc=self.panel_svc,
            password_svc=self.password_svc,
            audit_svc=self.audit_svc,
            bandwidth_svc=self.bandwidth_svc,
        )
        self.code_svc = AsyncCodeService(self.res, audit_svc=self.audit_svc)
        self.business_code_svc = AsyncBusinessCodeService(
            self.res,
            audit_svc=self.audit_svc,
            password_svc=self.password_svc,
            user_svc=self.business_svc,
        )
        self.telegram_svc = AsyncTelegramService(
            self.res, user_svc=self.business_svc, code_svc=self.code_svc
        )
        self.leaderboard_svc = AsyncLeaderboardService(
            self.res, user_svc=self.user_svc, bandwidth_svc=self.bandwidth_svc
        )
        self._close_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.business_code_svc.recover_rollback_failures()
        await self.business_svc.recover_pending()

    async def close(self) -> None:
        if self._close_task is None:
            self.res.coordinator.registry.stop_admission()
            self._close_task = asyncio.create_task(self._close(), name="core:close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        await self.res.coordinator.registry.drain()
        await self.panel_svc.close()
