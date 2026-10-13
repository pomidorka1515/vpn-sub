from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from paths import bundled_root, program_dir, runtime_dir

if TYPE_CHECKING:
    from collections.abc import Callable

    from api.decorators.rate_limit import AsyncRateLimiter
    from session.transport import XUiPanelTransport

type PanelTransportFactory = Callable[[], XUiPanelTransport]


def verbose_overrides(raw: str | None = None) -> frozenset[str]:
    if raw is None:
        raw = os.getenv("VERBOSE_LOGGING_OVERRIDES", "")
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


@dataclass(frozen=True, slots=True, kw_only=True)
class AppPaths:
    data: Path
    backups: Path
    config: Path
    language: Path
    database: Path
    log: Path
    audit: Path
    primary_lock: Path

    @classmethod
    def from_env(cls) -> AppPaths:
        data = Path(os.getenv("DIR_DATA", program_dir() / "data"))
        return cls(
            data=data,
            backups=Path(os.getenv("DIR_BACKUPS", data / "backup")),
            config=Path(os.getenv("PATH_CONFIG", data / "config.json")),
            language=Path(os.getenv("PATH_LANG", bundled_root() / "lang.jsonc")),
            database=Path(os.getenv("PATH_DB", data / "state.sqlite3")),
            log=Path(os.getenv("PATH_LOG", data / "log.jsonl")),
            audit=Path(os.getenv("PATH_AUDIT", data / "audit.jsonl")),
            primary_lock=runtime_dir(data) / ".primary.lock",
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AppOptions:
    start_background: bool = True
    start_bots: bool = True
    proxy_hops: int = 1
    require_proxy: bool = False
    panel_transport_factory: PanelTransportFactory | None = None
    limiter: AsyncRateLimiter | None = None
