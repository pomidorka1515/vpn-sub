import platform

from argon2 import PasswordHasher

from .common import SharedCoreResources
from errors import UnsupportedPlatformError
from config import ConfigLike, LinesConfigLike
from db import Database
from flask import Flask
from session import XUiSession
from loggers import Logger

from .services.password import PasswordService
from .services.audit import AuditService
from .services.panel import PanelService
from .services.telegram import TelegramService
from .services.leaderboard import LeaderboardService
from .services.bandwidth import BandwidthService
from .services.user.common  import CommonUserService
from .services.user.business import BusinessUserService
from .services.code import CodeService, BusinessCodeService

__all__ = ["Subscription"]

if platform.system().lower() != "linux":
    raise UnsupportedPlatformError()



class Subscription:
    def __init__(
        self,
        cfg: ConfigLike,
        lang_cfg: ConfigLike, # ro
        db: Database,
        app: Flask,
        panels: list[XUiSession],
        whitelist_panel: XUiSession | None,
        audit_cfg: LinesConfigLike | None = None
    ):
        log = Logger(type(self).__name__)
        with log.loading():
            self.res = SharedCoreResources(
                cfg=cfg, lang_cfg=lang_cfg, db=db,
                app=app, log=log,
                panels=panels,
                whitelist_panel=whitelist_panel,
                audit_cfg=audit_cfg,
                password_hasher=PasswordHasher(),
                legacy_salt=cfg['salt']
            )

            # Service creation

            self.audit_svc: AuditService = AuditService(self.res)
            self.panel_svc: PanelService = PanelService(self.res)
            self.password_svc: PasswordService = PasswordService(self.res)
            self.user_svc: CommonUserService = CommonUserService(self.res)
            self.bandwidth_svc: BandwidthService = BandwidthService(
                self.res, panel_svc=self.panel_svc, user_svc=self.user_svc
            )
            self.business_svc: BusinessUserService = BusinessUserService(
                self.res, user_svc=self.user_svc, panel_svc=self.panel_svc,
                password_svc=self.password_svc, audit_svc=self.audit_svc,
                bandwidth_svc=self.bandwidth_svc
            )
            self.code_svc: CodeService = CodeService(
                self.res, audit_svc=self.audit_svc
            )
            self.telegram_svc: TelegramService = TelegramService(
                self.res, code_svc=self.code_svc, user_svc=self.business_svc
            )
            self.business_code_svc: BusinessCodeService = BusinessCodeService(
                self.res, audit_svc=self.audit_svc, password_svc=self.password_svc,
                user_svc=self.business_svc
            )
            self.leaderboard_svc: LeaderboardService = LeaderboardService(
                self.res, user_svc=self.user_svc, bandwidth_svc=self.bandwidth_svc
            )
