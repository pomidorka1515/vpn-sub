from dataclasses import dataclass
from flask import Flask
from argon2 import PasswordHasher

from config import ConfigLike, LinesConfigLike
from db import Database
from session import XUiSession
from loggers import Logger

__all__ = ["SharedCoreResources", "BaseService"]

@dataclass(frozen=True, slots=True, kw_only=True)
class SharedCoreResources:
    log: Logger
    cfg: ConfigLike
    lang_cfg: ConfigLike
    audit_cfg: LinesConfigLike | None
    db: Database
    app: Flask
    legacy_salt: str
    panels: list[XUiSession]
    whitelist_panel: XUiSession | None
    password_hasher: PasswordHasher

class BaseService:
    def __init__(self, res: SharedCoreResources) -> None:
        self.res = res

    @property
    def log(self) -> Logger:
        return self.res.log

    @property
    def cfg(self) -> ConfigLike:
        return self.res.cfg

    @property
    def lang_cfg(self) -> ConfigLike:
        return self.res.lang_cfg

    @property
    def audit_cfg(self) -> LinesConfigLike | None:
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