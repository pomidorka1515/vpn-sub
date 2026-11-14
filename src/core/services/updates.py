from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from app_runtime.tasks import TaskRegistry
from custom_types import UserInfo, UserInfoBandwidth, UserInfoBandwidthTotal
from util import compare

from ..common import BaseService

if TYPE_CHECKING:
    from custom_types import UserRecord

    from ..common import SharedCoreResources
    from .panel import PanelService

UPDATE_INTERVAL = 2


@dataclass(frozen=True, slots=True)
class UpdateSnapshot:
    users: dict[str, UserRecord]
    frames: dict[str, bytes]

    def authorized_frame(self, username: str, auth_token: str) -> bytes | None:
        user = self.users.get(username)
        if (
            user is None
            or not compare(user["auth_token"] or "", auth_token)
            or (user["auth_token_expires_at"] or 0) <= time.time()
        ):
            return None
        return self.frames[username]


class UpdateService(BaseService):
    """One demand-driven batch per interval, independent of connected browsers.

    Two SQLite reads per batch (users and the panel online cache's usernames)
    keep sustained polling at at most one query/second. No per-user panel I/O,
    SQL, queues, or background work when nobody is watching. Failed batches are
    also shared until the interval expires, avoiding retry storms on outages.
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
            self.panel_svc.map_panels(self.panels, self.panel_svc.list_clients, work="background"),
            self.panel_svc.get_online_status(fresh=True, work="background"),
        )
        # Sample identity after panel I/O; never attach old counters to a reset UUID.
        users = {user["username"]: user for user in await self.db.list_user_records()}
        totals = {name: [0, 0, 0, 0] for name in users}
        for panel, rows in zip(self.panels, clients, strict=True):
            offset = 2 if panel is self.whitelist_panel else 0
            for client in rows:
                user = users.get(client.email)
                traffic = client.traffic
                if (
                    user is not None
                    and client.uuid == user["uuid"]
                    and traffic is not None
                    and traffic.uuid == user["uuid"]
                    and traffic.email == client.email
                ):
                    totals[client.email][offset] += traffic.up
                    totals[client.email][offset + 1] += traffic.down
        conf = self.cfg.view()
        frames: dict[str, bytes] = {}
        online_users = set(online.users)
        for name, user in users.items():
            up, down, wl_up, wl_down = totals[name]
            info = UserInfo(
                token=user["token"],
                link=f"{conf['domain']}/{conf['uri'].strip('/')}?token={user['token']}",
                displayname=user["displayname"],
                uuid=user["uuid"],
                fingerprint=user["fingerprint"],
                enabled=bool(user["enabled"]),
                wl_enabled=bool(user["enabled_wl"]),
                time=user["expires_at"],
                online=name in online_users,
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
            frames[name] = f"data: {json.dumps(asdict(info), separators=(',', ':'))}\n\n".encode()
        return UpdateSnapshot(users, frames)

    async def close(self) -> None:
        await self._tasks.drain()
