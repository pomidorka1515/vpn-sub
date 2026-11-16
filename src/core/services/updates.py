from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from app_runtime.tasks import TaskRegistry
from custom_types import UserInfo, UserInfoBandwidth, UserInfoBandwidthTotal
from util import compare

from ..common import BaseService

if TYPE_CHECKING:
    from custom_types import UpdateUser

    from ..common import SharedCoreResources
    from .panel import PanelService

UPDATE_INTERVAL = 2


@dataclass(frozen=True, slots=True)
class UpdateSnapshot:
    users: dict[str, UpdateUser]
    frames: dict[str, bytes]
    totals: dict[str, list[int]] = field(default_factory=dict[str, list[int]])
    online: frozenset[str] = frozenset()
    link_prefix: str = ""

    def authorized_frame(self, username: str, auth_token: str) -> bytes | None:
        user = self.users.get(username)
        if (
            user is None
            or not compare(user["auth_token"] or "", auth_token)
            or (user["auth_token_expires_at"] or 0) <= time.time()
        ):
            return None
        frame = self.frames.get(username)
        if frame is None:
            up, down, wl_up, wl_down = self.totals.get(username, (0, 0, 0, 0))
            info = UserInfo(
                token=user["token"],
                link=f"{self.link_prefix}{user['token']}",
                displayname=user["displayname"],
                uuid=user["uuid"],
                fingerprint=user["fingerprint"],
                enabled=bool(user["enabled"]),
                wl_enabled=bool(user["enabled_wl"]),
                time=user["expires_at"],
                online=username in self.online,
                bandwidth=UserInfoBandwidth(
                    total=UserInfoBandwidthTotal(upload=up, download=down, total=up + down),
                    wl_total=UserInfoBandwidthTotal(
                        upload=wl_up, download=wl_down, total=wl_up + wl_down
                    ),
                    monthly=user["bw_used"],
                    wl_monthly=user["wl_used"],
                    limit=user["bw_limit_gb"],
                    wl_limit=user["wl_limit_gb"],
                ),
            )
            frame = f"data: {json.dumps(asdict(info), separators=(',', ':'))}\n\n".encode()
            self.frames[username] = frame
        return frame


class UpdateService(BaseService):
    """One demand-driven batch per interval, independent of connected browsers.

    Panel traffic and online snapshots are shared with other consumers. Frames
    are built only for authorized viewers and cached within the batch. Failed
    batches are shared until the interval expires.
    """

    def __init__(self, res: SharedCoreResources, *, panel_svc: PanelService) -> None:
        super().__init__(res)
        self.panel_svc = panel_svc
        self._tasks = TaskRegistry(report_failures=False)
        self._task: asyncio.Task[UpdateSnapshot] | None = None
        self._stored_at = 0.0

    async def snapshot(self) -> UpdateSnapshot:
        if self._task is None or (
            self._task.done() and time.monotonic() - self._stored_at >= UPDATE_INTERVAL
        ):
            self._task = self._tasks.create(self._load, name="core:panel-updates")
        return await asyncio.shield(self._task)

    async def _load(self) -> UpdateSnapshot:
        try:
            return await self._collect()
        finally:
            self._stored_at = time.monotonic()

    async def _collect(self) -> UpdateSnapshot:
        self.require_panels()
        clients, online = await asyncio.gather(
            asyncio.gather(*(self.panel_svc.traffic_snapshot(panel) for panel in self.panels)),
            self.panel_svc.unfiltered_onlines(work="background"),
        )
        # Sample identity after panel I/O; never attach old counters to a reset UUID.
        users = {user["username"]: user for user in await self.db.list_update_users()}
        totals: dict[str, list[int]] = {}
        for panel, rows in zip(self.panels, clients, strict=True):
            offset = 2 if panel is self.whitelist_panel else 0
            for email, uuid, traffic_email, traffic_uuid, up, down in rows.rows:
                user = users.get(email)
                if (
                    user is not None
                    and uuid == user["uuid"]
                    and traffic_uuid == user["uuid"]
                    and traffic_email == email
                ):
                    total = totals.setdefault(email, [0, 0, 0, 0])
                    total[offset] += up
                    total[offset + 1] += down
        conf = self.cfg.view()
        return UpdateSnapshot(
            users,
            {},
            totals,
            frozenset(name for name in online[0] if name in users),
            f"{conf['domain']}/{conf['uri'].strip('/')}?token=",
        )

    async def close(self) -> None:
        await self._tasks.drain()
