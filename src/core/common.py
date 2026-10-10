from dataclasses import dataclass
from flask import Flask
from argon2 import PasswordHasher

from config import AppConfig, Config, LangConfig, JsonValue, LinesConfig
from db import Database
from session import XUiSession
from loggers import Logger
from tracer import trace, TraceOp

__all__ = ["SharedCoreResources", "BaseService"]

@dataclass(frozen=True, slots=True, kw_only=True)
class SharedCoreResources:
    log: Logger
    cfg: Config[AppConfig]
    lang_cfg: Config[LangConfig]
    audit_cfg: LinesConfig | None
    db: Database
    app: Flask
    legacy_salt: str
    panels: list[XUiSession]
    whitelist_panel: XUiSession | None
    password_hasher: PasswordHasher
    verbose: bool = False

class BaseService:
    def __init__(self, res: SharedCoreResources) -> None:
        self.res = res

    def trace(self, operation: TraceOp, event: str, **fields: JsonValue) -> None:
        """
        Trace a detailed operation if ``self.res.verbose`` is True.
        """
        if not self.res.verbose:
            return
        trace(self.log, operation, event, **fields)
    
    @property
    def log(self) -> Logger:
        return self.res.log

    @property
    def cfg(self) -> Config[AppConfig]:
        return self.res.cfg

    @property
    def lang_cfg(self) -> Config[LangConfig]:
        return self.res.lang_cfg

    @property
    def audit_cfg(self) -> LinesConfig | None:
        return self.res.audit_cfg

    @property
    def db(self) -> Database:
        return self.res.db

    @property
    def app(self) -> Flask:
        return self.res.app

    @property
    def legacy_salt(self) -> str:
        return self.res.legacy_salt

    @property
    def panels(self) -> list[XUiSession]:
        return self.res.panels

    @property
    def whitelist_panel(self) -> XUiSession | None:
        return self.res.whitelist_panel

    @property
    def password_hasher(self) -> PasswordHasher:
        return self.res.password_hasher
