from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import quote

from dacite import from_dict

from app_runtime.tasks import TaskRegistry
from custom_types import (
    ClientListResponse,
    ClientTraffic,
    Inbound,
    OnlineStatus,
    PanelClient,
    ServerMetricsResponse,
)
from errors import PanelRejectedError, PanelUnavailableError
from session.cache import GenerationCache
from session.stamp import stamp_path

from ..common import BaseService

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping, Sequence

    from session.client import XUiSession
    from session.transport import PanelResponse

    from ..common import SharedCoreResources

type WorkClass = Literal["foreground", "background"]
type Health = Literal["ok", "unavailable", "invalid"]
type TrafficRow = tuple[str, str, str, str, int, int]

TRAFFIC_TTL = 15
TRAFFIC_STALE_GRACE = 60
TRAFFIC_RETRY_DELAY = 5


@dataclass(frozen=True, slots=True)
class TrafficSnapshot:
    rows: tuple[TrafficRow, ...]
    by_email: dict[str, TrafficRow]
    all_by_email: dict[str, list[TrafficRow]]

    @classmethod
    def from_rows(cls, rows: list[TrafficRow]) -> TrafficSnapshot:
        grouped: dict[str, list[TrafficRow]] = {}
        for row in rows:
            grouped.setdefault(row[0], []).append(row)
        return cls(tuple(rows), {row[0]: row for row in rows}, grouped)


@dataclass(frozen=True, slots=True)
class PanelOutcome[T]:
    panel: str
    value: T | None = None
    error: Exception | None = None

    @property
    def state(self) -> Literal["accepted", "failed", "unknown"]:
        if self.error is None:
            return "accepted"
        return "failed" if isinstance(self.error, PanelRejectedError) else "unknown"


class PanelService(BaseService):
    """Bound worker sets; all outcomes settle before ordered failure reporting.

    Work-class admission precedes each panel client's total request admission.
    Fanout callbacks must not recursively call fanout. Direct reads use the
    client's total admission; fanout retains reserved foreground capacity.
    """

    def __init__(
        self,
        res: SharedCoreResources,
        *,
        foreground: int = 4,
        background: int = 4,
    ) -> None:
        super().__init__(res)
        if foreground < 1 or background < 1:
            raise ValueError("panel fanout limits must be positive")
        self._limits: dict[WorkClass, int] = {"foreground": foreground, "background": background}
        self._slots = {key: asyncio.Semaphore(value) for key, value in self._limits.items()}
        self._fanouts = {
            work: TaskRegistry(capacity=32, report_failures=False) for work in self._limits
        }
        self._refreshes = TaskRegistry(capacity=32, report_failures=False)
        self._inbounds: dict[str, GenerationCache[list[Inbound]]] = {}
        self._clients: dict[str, GenerationCache[dict[str, PanelClient]]] = {}
        disk_lock = asyncio.Lock()
        for panel in self.panels:
            self._inbounds[panel.name] = GenerationCache(
                stamp_path("inbounds", panel.name, panel.base_url, directory=panel.stamp_dir),
                files=res.offloads.files,
                tasks=self._refreshes,
                disk_lock=disk_lock,
            )
            self._clients[panel.name] = GenerationCache(
                stamp_path("clients", panel.name, panel.base_url, directory=panel.stamp_dir),
                files=res.offloads.files,
                tasks=self._refreshes,
                disk_lock=disk_lock,
            )
        self._online: tuple[dict[str, None], dict[str, Health], float] | None = None
        self._online_task: asyncio.Task[tuple[dict[str, None], dict[str, Health], float]] | None = (
            None
        )
        self._online_epoch = 0
        self._online_task_epoch = -1
        # Immutable rows, private maps: hits only look up their own row, never
        # copy or parse the full panel inventory. One refresh per panel at most.
        self._traffic: dict[str, tuple[float, TrafficSnapshot]] = {}
        self._traffic_epoch: dict[str, int] = {}
        self._traffic_tasks: dict[str, asyncio.Task[TrafficSnapshot]] = {}
        self._traffic_retry_at: dict[str, float] = {}
        self._traffic_refreshes = TaskRegistry()

    async def outcomes[T](
        self,
        panels: Sequence[XUiSession],
        fn: Callable[[XUiSession], Coroutine[object, object, T]],
        *,
        work: WorkClass = "foreground",
    ) -> list[PanelOutcome[T]]:
        async def collect() -> list[PanelOutcome[T]]:
            results: list[PanelOutcome[T] | None] = [None] * len(panels)
            indexes = iter(range(len(panels)))

            async def worker() -> None:
                for index in indexes:
                    panel = panels[index]
                    async with self._slots[work]:
                        try:
                            results[index] = PanelOutcome(panel.name, value=await fn(panel))
                        except Exception as exc:
                            results[index] = PanelOutcome(panel.name, error=exc)

            # Only a fixed worker set exists, even for large input sequences.
            async with asyncio.TaskGroup() as group:
                for index in range(min(len(panels), self._limits[work])):
                    group.create_task(worker(), name=f"panel:{work}:{index}")
            return [result for result in results if result is not None]

        return await asyncio.shield(
            self._fanouts[work].create(collect, name=f"panel:fanout:{work}"),
        )

    async def map_panels[T](
        self,
        panels: Sequence[XUiSession],
        fn: Callable[[XUiSession], Coroutine[object, object, T]],
        *,
        work: WorkClass = "foreground",
    ) -> list[T]:
        outcomes = await self.outcomes(panels, fn, work=work)
        for outcome in outcomes:
            if outcome.error is not None:
                raise outcome.error
        return [cast(T, outcome.value) for outcome in outcomes]

    @staticmethod
    def response_data(
        panel: XUiSession,
        response: PanelResponse,
        what: str,
    ) -> dict[str, object]:
        try:
            raw: object = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise PanelUnavailableError(f"Panel {panel.name} {what}: invalid JSON") from exc
        if response.status_code not in {200, 201}:
            raise PanelUnavailableError(f"Panel {panel.name} {what}: HTTP {response.status_code}")
        if not isinstance(raw, dict):
            raise PanelUnavailableError(f"Panel {panel.name} {what}: invalid payload")
        return cast(dict[str, object], raw)

    async def getstatus(self, panel: XUiSession) -> ServerMetricsResponse | None:
        try:
            return from_dict(
                ServerMetricsResponse,
                self.response_data(panel, await panel.get("panel/api/server/status"), "status"),
            )
        except Exception:
            self.log.exception("panel status is unknown for %s", panel.name)
            return None

    async def statuses(
        self,
        panels: Sequence[XUiSession] | None = None,
        *,
        work: WorkClass = "foreground",
    ) -> list[ServerMetricsResponse | None]:
        return await self.map_panels(
            self.panels if panels is None else panels,
            self.getstatus,
            work=work,
        )

    async def getinbounds(self, panel: XUiSession) -> list[Inbound]:
        async def load() -> list[Inbound]:
            data = self.response_data(
                panel,
                await panel.get("panel/api/inbounds/list"),
                "inbound query",
            )
            if not data.get("success"):
                raise PanelUnavailableError(f"Panel {panel.name} inbound query failed")
            try:
                rows = cast(list[dict[str, object]], data["obj"])
                inbounds = [from_dict(Inbound, row) for row in rows]
                return [
                    row
                    for row in inbounds
                    if (row.id in panel.inbounds_list) == (panel.mode == "whitelist")
                ]
            except Exception as exc:
                raise PanelUnavailableError(f"Panel {panel.name} invalid inbounds") from exc

        return await self._inbounds[panel.name].get(2 if panel.local else 15, load)

    async def list_clients(self, panel: XUiSession) -> list[PanelClient]:
        epoch = self._traffic_epoch.get(panel.name, 0)
        response = await panel.get("panel/api/clients/list")
        clients, snapshot = await self.res.offloads.rendering.run(
            self._parse_clients, panel, response
        )
        self._publish_traffic(panel, epoch, snapshot)
        return clients

    @classmethod
    def _parse_clients(
        cls, panel: XUiSession, response: PanelResponse
    ) -> tuple[list[PanelClient], TrafficSnapshot]:
        data = cls.response_data(panel, response, "client list")
        if not data.get("success"):
            raise PanelRejectedError(f"Panel {panel.name} client list failed: {data.get('msg')}")
        try:
            for item in cast(list[dict[str, object]], data["obj"]):
                if item.get("inboundIds") is None:
                    item["inboundIds"] = []
            clients = from_dict(ClientListResponse, data).obj
            rows = [
                (
                    client.email,
                    client.uuid,
                    client.traffic.email,
                    client.traffic.uuid,
                    client.traffic.up,
                    client.traffic.down,
                )
                for client in clients
                if client.traffic is not None
            ]
            return clients, TrafficSnapshot.from_rows(rows)
        except Exception as exc:
            raise PanelUnavailableError(f"Panel {panel.name} invalid client list") from exc

    async def list_update_traffic(self, panel: XUiSession) -> list[TrafficRow]:
        """Read only the identity and counters needed by dashboard updates."""
        return list((await self.traffic_snapshot(panel)).rows)

    async def _load_traffic(self, panel: XUiSession) -> tuple[list[TrafficRow], TrafficSnapshot]:
        epoch = self._traffic_epoch.get(panel.name, 0)
        response = await panel.get("panel/api/clients/list")
        rows, snapshot = await self.res.offloads.rendering.run(self._parse_traffic, panel, response)
        self._publish_traffic(panel, epoch, snapshot)
        return rows, snapshot

    def _publish_traffic(self, panel: XUiSession, epoch: int, snapshot: TrafficSnapshot) -> None:
        if epoch == self._traffic_epoch.get(panel.name, 0):
            self._traffic[panel.name] = (monotonic(), snapshot)
            self._traffic_retry_at.pop(panel.name, None)

    @classmethod
    def _parse_traffic(
        cls, panel: XUiSession, response: PanelResponse
    ) -> tuple[list[TrafficRow], TrafficSnapshot]:
        data = cls.response_data(panel, response, "client list")
        if not data.get("success"):
            raise PanelRejectedError(f"Panel {panel.name} client list failed: {data.get('msg')}")
        try:
            rows = data["obj"]
            if not isinstance(rows, list):
                raise ValueError("invalid client list")
            result: list[TrafficRow] = []
            for raw in cast(list[object], rows):
                if not isinstance(raw, dict):
                    raise ValueError("invalid client")
                client = cast(dict[str, object], raw)
                email, uuid, traffic = client["email"], client["uuid"], client.get("traffic")
                if not isinstance(email, str) or not isinstance(uuid, str):
                    raise ValueError("invalid client identity")
                if traffic is None:
                    continue
                if not isinstance(traffic, dict):
                    raise ValueError("invalid traffic")
                values = cast(dict[str, object], traffic)
                traffic_email, traffic_uuid = values["email"], values["uuid"]
                up, down = values["up"], values["down"]
                if not (
                    isinstance(traffic_email, str)
                    and isinstance(traffic_uuid, str)
                    and isinstance(up, int)
                    and isinstance(down, int)
                ):
                    raise ValueError("invalid traffic fields")
                result.append((email, uuid, traffic_email, traffic_uuid, up, down))
            return result, TrafficSnapshot.from_rows(result)
        except Exception as exc:
            raise PanelUnavailableError(f"Panel {panel.name} invalid client list") from exc

    async def traffic_snapshot(
        self, panel: XUiSession, *, work: WorkClass = "background", allow_stale: bool = True
    ) -> TrafficSnapshot:
        """Share a 15-second sample across traffic consumers.

        Cold/expired reads coalesce before fanout admission, so reconnect storms
        cannot consume one fanout slot (or panel request) per subscription hit.
        On refresh failure, optionally serve a sample for up to 60 seconds past expiry,
        and reuse the failure for five seconds before retrying.
        Mutation invalidation prevents an in-flight old generation being reused.
        """
        cached = self._traffic.get(panel.name)
        if cached is not None and monotonic() - cached[0] < TRAFFIC_TTL:
            return cached[1]
        task = self._traffic_tasks.get(panel.name)
        if task is None or (
            task.done() and monotonic() >= self._traffic_retry_at.get(panel.name, 0)
        ):
            epoch = self._traffic_epoch.get(panel.name, 0)

            async def refresh() -> TrafficSnapshot:
                try:
                    results = await self.map_panels([panel], self._load_traffic, work=work)
                    return results[0][1]
                except Exception:
                    if epoch == self._traffic_epoch.get(panel.name, 0):
                        self._traffic_retry_at[panel.name] = monotonic() + TRAFFIC_RETRY_DELAY
                    raise

            task = self._traffic_refreshes.create(refresh, name="panel:subscription-traffic")
            self._traffic_tasks[panel.name] = task
        try:
            return await asyncio.shield(task)
        except Exception:
            cached = self._traffic.get(panel.name)
            if (
                allow_stale
                and cached is not None
                and monotonic() - cached[0] < TRAFFIC_TTL + TRAFFIC_STALE_GRACE
            ):
                return cached[1]
            raise

    async def subscription_traffic(self, panel: XUiSession, email: str) -> TrafficRow | None:
        return (await self.traffic_snapshot(panel)).by_email.get(email)

    async def clients_snapshot(self, panel: XUiSession) -> Mapping[str, PanelClient]:
        async def load() -> dict[str, PanelClient]:
            return {client.email: client for client in await self.list_clients(panel)}

        return await self._clients[panel.name].get(4, load)

    async def client_maps(
        self,
        panels: Sequence[XUiSession],
        *,
        work: WorkClass = "foreground",
    ) -> dict[str, Mapping[str, PanelClient]]:
        snapshots = await self.map_panels(panels, self.clients_snapshot, work=work)
        return {panel.name: snapshot for panel, snapshot in zip(panels, snapshots, strict=True)}

    async def invalidate_clients(self, panel: XUiSession) -> None:
        self._online_epoch += 1
        self._online = None
        self._traffic_epoch[panel.name] = self._traffic_epoch.get(panel.name, 0) + 1
        self._traffic.pop(panel.name, None)
        if monotonic() >= self._traffic_retry_at.get(panel.name, 0):
            self._traffic_tasks.pop(panel.name, None)
        await self._clients[panel.name].clear()

    async def invalidate_inbounds(self, panel: XUiSession) -> None:
        await self._inbounds[panel.name].clear()

    async def get_client(self, panel: XUiSession, email: str) -> PanelClient | None:
        data = self.response_data(
            panel,
            await panel.get(f"panel/api/clients/get/{quote(email, safe='')}"),
            "client query",
        )
        if not data.get("success"):
            if "record not found" in str(data.get("msg", "")).lower():
                return None
            raise PanelRejectedError(f"Panel {panel.name} client query failed: {data.get('msg')}")
        raw = data.get("obj")
        if raw is None:
            return None
        try:
            obj = cast(dict[str, object], raw)
            merged = dict(cast(dict[str, object], obj.get("client", obj)))
            if "inboundIds" in obj:
                merged.setdefault("inboundIds", obj["inboundIds"] or [])
            return from_dict(PanelClient, merged)
        except Exception as exc:
            raise PanelUnavailableError(f"Panel {panel.name} invalid client") from exc

    async def client_traffic(self, panel: XUiSession, email: str) -> ClientTraffic | None:
        data = self.response_data(
            panel,
            await panel.get(f"panel/api/clients/traffic/{quote(email, safe='')}"),
            "traffic query",
        )
        if not data.get("success"):
            raise PanelRejectedError(f"Panel {panel.name} traffic query failed: {data.get('msg')}")
        raw = data.get("obj")
        if raw is None:
            return None
        try:
            return from_dict(ClientTraffic, cast(dict[str, object], raw))
        except Exception as exc:
            raise PanelUnavailableError(f"Panel {panel.name} invalid traffic") from exc

    async def _load_onlines(
        self,
        epoch: int,
        work: WorkClass,
    ) -> tuple[dict[str, None], dict[str, Health], float]:
        users, health = await self._query_onlines(work=work)
        snapshot = users, health, monotonic()
        if epoch == self._online_epoch:
            self._online = snapshot
        return snapshot

    async def unfiltered_onlines(
        self, *, work: WorkClass = "foreground", fresh: bool = False
    ) -> tuple[dict[str, None], dict[str, Health]]:
        """Share online lists; callers must filter against known users."""
        users, health = await self.online_snapshot(work=work, fresh=fresh)
        return dict(users), dict(health)

    async def online_snapshot(
        self, *, work: WorkClass = "foreground", fresh: bool = False
    ) -> tuple[Mapping[str, None], Mapping[str, Health]]:
        """Return the shared online sample without copying its user index."""
        snapshot = self._online
        if fresh or snapshot is None or monotonic() - snapshot[2] >= 5:
            if (
                self._online_task is None
                or self._online_task.done()
                or self._online_task_epoch != self._online_epoch
            ):
                epoch = self._online_epoch
                self._online_task_epoch = epoch
                self._online_task = self._refreshes.create(
                    lambda: self._load_onlines(epoch, work),
                    name="panel:online-refresh",
                )
            snapshot = await asyncio.shield(self._online_task)
        users, health, _ = snapshot
        return users, health

    async def _query_onlines(self, *, work: WorkClass) -> tuple[dict[str, None], dict[str, Health]]:

        async def fetch(panel: XUiSession) -> dict[str, object]:
            return self.response_data(
                panel,
                await panel.post("panel/api/clients/onlines"),
                "online query",
            )

        results = await self.outcomes(self.panels, fetch, work=work)
        users: dict[str, None] = {}
        health: dict[str, Health] = {}
        for result in results:
            data = result.value
            if result.error is not None or data is None or not data.get("success"):
                health[result.panel] = "unavailable"
                continue
            raw = data.get("obj", [])
            health[result.panel] = "ok"
            if not isinstance(raw, list):
                health[result.panel] = "invalid"
                continue
            for email in cast(list[object], raw):
                if not isinstance(email, str):
                    health[result.panel] = "invalid"
                    break
                users.setdefault(email, None)
        if health and all(value == "unavailable" for value in health.values()):
            raise PanelUnavailableError("No panel could be queried for online users")
        return users, health

    async def get_online_status(
        self,
        new: bool = False,
        *,
        fresh: bool = False,
        work: WorkClass = "foreground",
    ) -> OnlineStatus:
        self.require_panels()
        users, health = await self.unfiltered_onlines(work=work, fresh=fresh)
        known = await self.db.usernames()
        users = {user: None for user in users if user in known}
        if new:
            exts = await self.db.username_exts()
            return OnlineStatus({user: exts.get(user) for user in users}, dict(health))
        return OnlineStatus(list(users), dict(health))

    async def get_online_users(self, new: bool = False) -> list[str] | dict[str, str | None]:
        return (await self.get_online_status(new)).users

    async def is_online(self, username: str) -> bool:
        status = await self.get_online_status()
        return username in status.users and any(
            value == "ok" for value in status.panel_health.values()
        )

    async def close(self) -> None:
        # Refreshes may own fanout work, so settle them before closing admission.
        await self._traffic_refreshes.drain()
        await self._refreshes.drain()
        for registry in self._fanouts.values():
            await registry.drain()
