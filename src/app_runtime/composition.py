from __future__ import annotations

import fcntl
from typing import TYPE_CHECKING, BinaryIO, cast

from session.async_client import AsyncXUiSession

if TYPE_CHECKING:
    from pathlib import Path

    from config import AppConfig
    from core.async_common import Snapshot

    from .settings import AppOptions, AppPaths


class PrimaryLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = None

    async def start(self) -> None:
        if self._handle is not None:
            return
        import asyncio

        def acquire() -> BinaryIO:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                raise RuntimeError("another application instance holds the primary lock") from None
            return handle

        self._handle = await asyncio.to_thread(acquire)

    async def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


class SnapshotStatus:
    """HTTP-facing snapshot status until the async watcher is composed in step 8."""

    def get_daily_snapshot_failure(self) -> dict[str, str | int] | None:
        return None


def build_panels(
    cfg: Snapshot[AppConfig], paths: AppPaths, options: AppOptions, *, verbose: bool
) -> tuple[list[AsyncXUiSession], AsyncXUiSession | None]:
    panels: list[AsyncXUiSession] = []
    whitelist: AsyncXUiSession | None = None
    for section in cfg.view()["3xui"].values():
        panel = AsyncXUiSession(
            name=section["name"],
            address=section["address"],
            port=section["port"],
            uri=section["uri"],
            api_token=section["token"],
            https=section["https"],
            nginx_auth=cast(tuple[str, str] | None, tuple(section.get("nginx_auth", [])) or None),
            inbounds_list=tuple(section["inbounds_list"]),
            mode=section["mode"],
            inject_headers=section.get("inject_headers"),
            transport=options.panel_transport_factory()
            if options.panel_transport_factory
            else None,
            stamp_dir=str(paths.primary_lock.parent),
            verbose=verbose,
        )
        if section["whitelist"]:
            whitelist = panel
        else:
            panels.append(panel)
    return panels, whitelist
