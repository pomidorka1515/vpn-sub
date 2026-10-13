from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from tracer import trace

if TYPE_CHECKING:
    from argon2 import PasswordHasher

    from app_runtime.offload import OffloadServices
    from config import AppConfig, JsonValue, LangConfig
    from db.database import Database
    from loggers import Logger
    from session.client import XUiSession
    from tracer import TraceOp

    from .coordination import MutationCoordinator


class Snapshot[Doc](Protocol):
    def view(self) -> Doc: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class SharedCoreResources:
    log: Logger
    cfg: Snapshot[AppConfig]
    lang_cfg: Snapshot[LangConfig]
    db: Database
    panels: list[XUiSession]
    whitelist_panel: XUiSession | None
    password_hasher: PasswordHasher
    legacy_salt: str
    offloads: OffloadServices
    coordinator: MutationCoordinator
    verbose: bool = False


class BaseService:
    def __init__(self, res: SharedCoreResources) -> None:
        self.res = res

    def trace(self, operation: TraceOp, event: str, **fields: JsonValue) -> None:
        if self.res.verbose:
            trace(self.res.log, operation, event, **fields)

    @property
    def log(self) -> Logger:
        return self.res.log

    @property
    def cfg(self) -> Snapshot[AppConfig]:
        return self.res.cfg

    @property
    def db(self) -> Database:
        return self.res.db

    @property
    def panels(self) -> list[XUiSession]:
        return self.res.panels

    @property
    def whitelist_panel(self) -> XUiSession | None:
        return self.res.whitelist_panel
