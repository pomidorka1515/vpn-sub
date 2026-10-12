from __future__ import annotations

import platform
from typing import TYPE_CHECKING

from argon2 import PasswordHasher

from errors import UnsupportedPlatformError
from loggers import Logger

from .common import SharedCoreResources
from .services import (
    AuditService,
    BandwidthService,
    BusinessCodeService,
    BusinessUserService,
    CodeService,
    CommonUserService,
    LeaderboardService,
    PanelService,
    PasswordService,
    TelegramService,
)

if TYPE_CHECKING:
    from config import AppConfig, Config, LangConfig, LinesConfig
    from db import Database
    from session import XUiSession

__all__ = ["Subscription"]

if platform.system().lower() != "linux":
    raise UnsupportedPlatformError


class Subscription:
    def __init__(
        self,
        cfg: Config[AppConfig],
        lang_cfg: Config[LangConfig],  # ro
        db: Database,
        panels: list[XUiSession],
        whitelist_panel: XUiSession | None,
        audit_cfg: LinesConfig | None = None,
        verbose: bool = False,
    ) -> None:
        log = Logger(type(self).__name__)
        with log.loading():
            self.panels = panels
            if whitelist_panel:
                self.panels.append(whitelist_panel)

            self.res = SharedCoreResources(
                cfg=cfg,
                lang_cfg=lang_cfg,
                db=db,
                log=log,
                panels=panels,
                whitelist_panel=whitelist_panel,
                audit_cfg=audit_cfg,
                password_hasher=PasswordHasher(),
                legacy_salt=cfg.view()["salt"],
                verbose=verbose,
            )

            # Service creation

            self.audit_svc: AuditService = AuditService(self.res)
            self.panel_svc: PanelService = PanelService(self.res)
            self.password_svc: PasswordService = PasswordService(self.res)
            self.user_svc: CommonUserService = CommonUserService(self.res)
            self.bandwidth_svc: BandwidthService = BandwidthService(
                self.res,
                panel_svc=self.panel_svc,
                user_svc=self.user_svc,
            )
            self.business_svc: BusinessUserService = BusinessUserService(
                self.res,
                user_svc=self.user_svc,
                panel_svc=self.panel_svc,
                password_svc=self.password_svc,
                audit_svc=self.audit_svc,
                bandwidth_svc=self.bandwidth_svc,
            )
            self.code_svc: CodeService = CodeService(self.res, audit_svc=self.audit_svc)
            self.telegram_svc: TelegramService = TelegramService(
                self.res,
                code_svc=self.code_svc,
                user_svc=self.business_svc,
            )
            self.business_code_svc: BusinessCodeService = BusinessCodeService(
                self.res,
                audit_svc=self.audit_svc,
                password_svc=self.password_svc,
                user_svc=self.business_svc,
            )
            self.leaderboard_svc: LeaderboardService = LeaderboardService(
                self.res,
                user_svc=self.user_svc,
                bandwidth_svc=self.bandwidth_svc,
            )
