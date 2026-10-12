from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from tracer import trace

if TYPE_CHECKING:
    from argon2 import PasswordHasher

    from app_runtime.offload import OffloadServices
    from config import AppConfig, JsonValue, LangConfig
    from db.async_database import AsyncDatabase
    from loggers import Logger
    from session.async_client import AsyncXUiSession
    from tracer import TraceOp

    from .coordination import MutationCoordinator


class Snapshot[Doc](Protocol):
    def view(self) -> Doc: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class AsyncCoreResources:
    log: Logger
    cfg: Snapshot[AppConfig]
    lang_cfg: Snapshot[LangConfig]
    db: AsyncDatabase
    panels: list[AsyncXUiSession]
    whitelist_panel: AsyncXUiSession | None
    password_hasher: PasswordHasher
    legacy_salt: str
    offloads: OffloadServices
    coordinator: MutationCoordinator
    verbose: bool = False


class AsyncBaseService:
    def __init__(self, res: AsyncCoreResources) -> None:
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
    def db(self) -> AsyncDatabase:
        return self.res.db

    @property
    def panels(self) -> list[AsyncXUiSession]:
        return self.res.panels

    @property
    def whitelist_panel(self) -> AsyncXUiSession | None:
        return self.res.whitelist_panel
