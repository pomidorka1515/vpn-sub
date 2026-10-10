from __future__ import annotations

from concurrent.futures import Executor, Future, ThreadPoolExecutor
from collections.abc import Callable, Mapping, Sequence
from threading import Condition
from time import monotonic
from urllib.parse import quote
from dacite import from_dict
from typing import Literal, cast, overload

from requests import Response

from ..common import BaseService, SharedCoreResources
from tracer import Op
from session import XUiSession
from custom_types import (
    ClientListResponse,
    ClientTraffic,
    Inbound,
    OnlineStatus,
    PanelClient,
    ServerMetricsResponse,
)
from errors import AppError, PanelRejectedError, PanelUnavailableError

__all__ = ["BG_POOL", "PanelService"]

_PANEL_POOL_WORKERS = 4
_BG_POOL_WORKERS = 4
_CLIENTS_TTL = 4.0
_ONLINES_TTL = 4.0
_PANEL_POOL = ThreadPoolExecutor(
    max_workers=_PANEL_POOL_WORKERS, thread_name_prefix="panel-req"
)
_BG_POOL = ThreadPoolExecutor(
    max_workers=_BG_POOL_WORKERS, thread_name_prefix="panel-bg"
)
BG_POOL: Executor = _BG_POOL

class _OnlineSnapshot:
    """Classified online set. Ext names are applied later, so a rename is live."""

    __slots__ = ("users", "panel_health", "stored_at")

    def __init__(
        self,
        users: dict[str, None],
        panel_health: dict[str, Literal["ok", "unavailable", "invalid"]],
        stored_at: float,
    ) -> None:
        self.users = users
        self.panel_health = panel_health
        self.stored_at = stored_at


class PanelService(BaseService):
    def __init__(self, res: SharedCoreResources) -> None:
        super().__init__(res)
        self._onlines_lock = Condition()
        self._onlines: _OnlineSnapshot | None = None
        self._onlines_loading = False

    def map_panels[T](
        self,
        panels: Sequence[XUiSession],
        fn: Callable[[XUiSession], T],
        pool: Executor | None = None,
    ) -> list[T]:
        """Run ``fn`` on each panel. Results follow ``panels`` order.

        Wall time is the slowest call, not the sum. An exception is raised
        from the first failing panel in list order and does not cancel the
        others. ``fn`` must not call ``map_panels`` on either pool: a worker
        that waits still occupies its slot, and a nested submit deadlocks
        once that pool is full. Cross-pool waiting is safe only one way
        (request never waits on background, background never waits on
        request). Zero or one panel runs on the caller thread and does not
        occupy a worker.

        ``pool`` defaults to the request executor. Background callers
        (BWatch, admin leaderboard) pass ``BG_POOL``. Request handlers
        pass nothing.
        """
        started = monotonic()
        names = [panel.name for panel in panels]
        executor = _PANEL_POOL if pool is None else pool
        executor_name = getattr(executor, "_thread_name_prefix", type(executor).__name__)
        self.trace(
            Op.panel.map_panels, "start",
            panels=names, count=len(panels), executor=executor_name,
            inline=len(panels) <= 1,
        )
        if len(panels) <= 1:
            try:
                results = [fn(panel) for panel in panels]
            except Exception as exc:
                self.trace(
                    Op.panel.map_panels, "failed",
                    panels=names, executor=executor_name, inline=True,
                    duration=round(monotonic() - started, 6),
                    error=type(exc).__name__,
                )
                raise
            self.trace(
                Op.panel.map_panels, "done",
                panels=names, executor=executor_name, inline=True,
                duration=round(monotonic() - started, 6),
                outcomes=len(results),
            )
            return results
        futures: list[Future[T]] = [
            executor.submit(fn, panel) for panel in panels
        ]
        # result() in input order: Executor.map would cancel not-yet-started
        # siblings on the first exception and drop a later panel's work.
        # The first failure in list order still raises immediately. Later
        # futures keep running; only the outcomes already observed are traced.
        outcomes: list[str] = []
        gathered: list[T] = []
        try:
            for panel, future in zip(panels, futures, strict=True):
                try:
                    gathered.append(future.result())
                except BaseException as exc:
                    outcomes.append(f"{panel.name}:{type(exc).__name__}")
                    raise
                outcomes.append(f"{panel.name}:ok")
        except BaseException as exc:
            self.trace(
                Op.panel.map_panels, "failed",
                panels=names, executor=executor_name, inline=False,
                duration=round(monotonic() - started, 6),
                outcomes=outcomes, error=type(exc).__name__,
            )
            raise
        self.trace(
            Op.panel.map_panels, "done",
            panels=names, executor=executor_name, inline=False,
            duration=round(monotonic() - started, 6),
            outcomes=outcomes,
        )
        return gathered

    def statuses(
        self,
        panels: Sequence[XUiSession] | None = None,
        pool: Executor | None = None,
    ) -> list[ServerMetricsResponse | None]:
        """Fan out ``getstatus``. Results align with the input sequence."""
        target = self.panels if panels is None else panels
        return self.map_panels(target, self.getstatus, pool)

    def getstatus(self, panel: XUiSession) -> ServerMetricsResponse | None:
        """Get panel status, or ``None`` when the panel status is unknown."""
        try:
            response = panel.get("panel/api/server/status")
            data: dict[str, object] = response.json()
            if response.status_code != 200:
                message = data.get("msg") or response.status_code
                raise PanelUnavailableError(f"Panel {panel.name} status query failed: {message}")
            result = from_dict(ServerMetricsResponse, data)
            obj = result.obj
            self.trace(
                Op.panel.getstatus, "ok",
                panel=panel.name, success=result.success, msg=result.msg,
                cpu=obj.cpu, cpu_cores=obj.cpuCores, logical_pro=obj.logicalPro,
                cpu_speed_mhz=obj.cpuSpeedMhz, mem_current=obj.mem.current,
                mem_total=obj.mem.total, swap_current=obj.swap.current,
                swap_total=obj.swap.total, disk_current=obj.disk.current,
                disk_total=obj.disk.total, xray_state=obj.xray.state,
                xray_error=obj.xray.errorMsg, xray_version=obj.xray.version,
                uptime=obj.uptime, loads=obj.loads, tcp=obj.tcpCount,
                udp=obj.udpCount, net_up=obj.netIO.up, net_down=obj.netIO.down,
                sent=obj.netTraffic.sent, recv=obj.netTraffic.recv,
                ipv4=obj.publicIP.ipv4, ipv6=obj.publicIP.ipv6,
                threads=obj.appStats.threads, app_mem=obj.appStats.mem,
                app_uptime=obj.appStats.uptime,
            )
            return result
        except Exception as exc:
            self.log.error("panel status is unknown for %s", panel.name, exc_info=True)
            self.trace(
                Op.panel.getstatus, "unknown",
                panel=panel.name, error=type(exc).__name__,
            )
            return None

    def getinbounds(self, panel: XUiSession) -> list[Inbound]:
        """Get inbounds list. Uses cache with TTL, panel.local (almost) skips cache.

        Filters by ``panel.mode`` and ``panel.inbounds_list``: whitelist keeps
        only listed IDs, blacklist drops them.
        """
        ttl = 2 if panel.local else 15  # fast local, slow remote

        cached = panel.fresh_cache(ttl)
        if cached is not None:
            self.trace(
                Op.panel.getinbounds, "hit",
                panel=panel.name, mode=panel.mode, ttl=ttl, count=len(cached),
            )
            return cached  # NOTE: cache stores dataclasses!

        try:
            response = panel.get("panel/api/inbounds/list")
            data: dict[str, list[dict[str, object]]] = response.json()
            if response.status_code not in (200,) or not data.get("success"):
                raise PanelUnavailableError(
                    f"Panel {panel.name} inbound query failed: "
                    f"{data.get('msg') or response.status_code}"
                )
            raw_inbounds: list[dict[str, object]] = data['obj']
            raw_count = len(raw_inbounds)
            inbounds = [from_dict(Inbound, i) for i in raw_inbounds]
            listed = set(panel.inbounds_list)
            if panel.mode == "whitelist":
                inbounds = [i for i in inbounds if i.id in listed]
            else:
                inbounds = [i for i in inbounds if i.id not in listed]
            panel.cache = inbounds
            self.trace(
                Op.panel.getinbounds, "miss",
                panel=panel.name, mode=panel.mode, ttl=ttl,
                raw=raw_count, listed=len(listed), kept=len(inbounds),
                dropped=raw_count - len(inbounds),
            )
            return inbounds
        except AppError:
            raise
        except Exception as exc:
            raise PanelUnavailableError(
                f"Panel {panel.name} inbound query failed: {exc}"
            ) from exc

    def clients_snapshot(self, panel: XUiSession) -> Mapping[str, PanelClient]:
        """Cached email-to-client map for "does this client exist, which inbounds".

        One ``clients/list`` per panel per TTL, not one ``clients/get`` per
        user. The map is built once when filling. TTL is a few seconds: long
        enough that one reconcile shares a list, short enough that a missed
        invalidation cannot keep a just-deleted user enabled. Not the inbound
        cache, and not a traffic source — ``client_traffic`` stays live.

        Callers that mutate must ``invalidate_clients`` after the POST. A
        later read in the same call must not be served the pre-write map.
        """
        cached = panel.fresh_clients(_CLIENTS_TTL)
        if cached is not None:
            self.trace(
                Op.panel.clients_snapshot, "hit",
                panel=panel.name, ttl=_CLIENTS_TTL, count=len(cached),
            )
            return cached
        clients = self.list_clients(panel)
        snapshot = {client.email: client for client in clients}
        panel.clients_cache = snapshot
        stored = panel.fresh_clients(_CLIENTS_TTL)
        # a clear that landed during the list wins: do not hand back the map
        # this fill just refused to store
        returned = stored if stored is not None else snapshot
        self.trace(
            Op.panel.clients_snapshot, "miss",
            panel=panel.name, ttl=_CLIENTS_TTL, count=len(returned),
            stored=stored is not None,
        )
        return returned

    def invalidate_clients(self, panel: XUiSession) -> None:
        """Drop this panel's client map. Call after every successful client POST."""
        panel.clear_clients()
        self.trace(Op.panel.invalidate_clients, "invalidated", panel=panel.name)

    def client_maps(
        self, panels: Sequence[XUiSession],
    ) -> dict[str, Mapping[str, PanelClient]]:
        """One client list per panel, filled on this thread before any writes.

        Bulk callers (reconcile, admin refresh) capture this once and pass
        the maps into each user. Workers only read. With more than one panel
        the lists themselves go through ``map_panels``; a worker must not
        call this. Keyed by panel name, which is unique in config.
        """
        if not panels:
            return {}
        snapshots = self.map_panels(panels, self.clients_snapshot)
        return {
            panel.name: snapshot
            for panel, snapshot in zip(panels, snapshots, strict=True)
        }

    def _status_error(self, panel: XUiSession, what: str, response: Response) -> PanelUnavailableError:
        """Classify a non-200 response as unavailability.

        The synthetic 503 from a dead ``XUiSession`` carries the down reason
        in ``msg`` — surface it like ``getinbounds`` does. A 404 means a
        missing/renamed route until a captured body proves otherwise, so it
        is an error, not "client absent".
        """
        message: object = response.status_code
        try:
            data: object = response.json()
            if isinstance(data, dict):
                message = cast(dict[str, object], data).get("msg") or response.status_code
        except Exception:
            pass
        return PanelUnavailableError(
            f"Panel {panel.name} {what} failed: {message}"
        )

    def get_client(self, panel: XUiSession, email: str) -> PanelClient | None:
        """Get a panel client by email, or ``None`` when the panel reports one.

        Not-found contract: the clients-first API answers ``success: false``
        with ``Obtain (record not found)`` (or a ``null`` ``obj``) on HTTP
        200 — both mean the client is unknown. A non-200 status (404
        included) is an availability error — a missing route must not look
        like an absent client. Any other ``success: false`` is a rejection
        and carries the panel ``msg``.

        Found payloads are wrapped: ``obj.client`` holds the client row and
        ``obj.inboundIds`` its attachments. Flat ``obj`` payloads (older
        panels, tests) are still accepted.
        """
        url = f"panel/api/clients/get/{quote(email, safe='')}"
        try:
            response = panel.get(url)
            if response.status_code != 200:
                raise self._status_error(panel, "client query", response)
            data: dict[str, object] = response.json()
            if not data.get("success"):
                message = str(data.get("msg") or response.status_code)
                if "record not found" in message.lower():
                    self.trace(
                        Op.panel.get_client, "absent",
                        panel=panel.name, email=email, msg=message,
                    )
                    return None
                raise PanelRejectedError(
                    f"Panel {panel.name} client query failed: {message}"
                )
            raw: object = data.get("obj")
            if raw is None:
                self.trace(
                    Op.panel.get_client, "absent",
                    panel=panel.name, email=email,
                )
                return None
            if not isinstance(raw, dict):
                raise PanelUnavailableError(
                    f"Panel {panel.name} client query failed: obj is not an object"
                )
            obj = cast(dict[str, object], raw)
            client_raw: object = obj.get("client", obj)
            if not isinstance(client_raw, dict):
                raise PanelUnavailableError(
                    f"Panel {panel.name} client query failed: obj.client is not an object"
                )
            merged: dict[str, object] = dict(cast(dict[str, object], client_raw))
            inbound_ids_raw: object = obj.get("inboundIds")
            if isinstance(inbound_ids_raw, list) and "inboundIds" not in merged:
                merged["inboundIds"] = list(cast(list[object], inbound_ids_raw))
            client = from_dict(PanelClient, merged)
            self.trace(
                Op.panel.get_client, "found",
                panel=panel.name, email=email, uuid=client.uuid,
                enable=client.enable, flow=client.flow,
                limit_ip=client.limitIp, total_gb=client.totalGB,
                expiry_time=client.expiryTime, tg_id=str(client.tgId),
                comment=client.comment, reset=client.reset,
                inbound_ids=client.inboundIds,
            )
            return client
        except AppError as exc:
            if isinstance(exc, PanelRejectedError):
                self.trace(
                    Op.panel.get_client, "rejected",
                    panel=panel.name, email=email, error=exc.message,
                )
            raise
        except Exception as exc:
            raise PanelUnavailableError(
                f"Panel {panel.name} client query failed: {exc}"
            ) from exc

    def list_clients(self, panel: XUiSession) -> list[PanelClient]:
        """List every panel client with its inbound attachments and traffic."""
        try:
            response = panel.get("panel/api/clients/list")
            if response.status_code != 200:
                raise self._status_error(panel, "client list query", response)
            data: dict[str, object] = response.json()
            if not data.get("success"):
                raise PanelRejectedError(
                    f"Panel {panel.name} client list query failed: "
                    f"{data.get('msg') or response.status_code}"
                )
            # unattached clients come back as inboundIds: null
            # dacite rejects that against list[int] and would fail the
            # whole listing including every attached client's traffic.
            # clients/get already reports the same clients as [].
            raw_obj: object = data.get("obj")
            if isinstance(raw_obj, list):
                for item in cast(list[object], raw_obj):
                    if not isinstance(item, dict):
                        continue
                    client = cast(dict[object, object], item)
                    if client.get("inboundIds") is None:
                        cast(dict[str, object], item)["inboundIds"] = []
            clients = from_dict(ClientListResponse, data).obj
            self.trace(
                Op.panel.list_clients, "ok",
                panel=panel.name, count=len(clients),
            )
            return clients
        except AppError:
            raise
        except Exception as exc:
            raise PanelUnavailableError(
                f"Panel {panel.name} client list query failed: {exc}"
            ) from exc

    def client_traffic(self, panel: XUiSession, email: str) -> ClientTraffic | None:
        """Get the client's single shared traffic row, or ``None`` when absent.

        The clients-first API keeps one ``client_traffics`` row per client
        (keyed by email) shared across every attached inbound — callers must
        NOT sum it over inbounds. A non-200 status (404 included) is an
        availability error, not "no traffic row".
        """
        url = f"panel/api/clients/traffic/{quote(email, safe='')}"
        try:
            response = panel.get(url)
            if response.status_code != 200:
                raise self._status_error(panel, "traffic query", response)
            data: dict[str, object] = response.json()
            if not data.get("success"):
                raise PanelRejectedError(
                    f"Panel {panel.name} traffic query failed: "
                    f"{data.get('msg') or response.status_code}"
                )
            raw: object = data.get("obj")
            if raw is None:
                self.trace(
                    Op.panel.client_traffic, "absent",
                    panel=panel.name, email=email,
                )
                return None
            traffic = from_dict(ClientTraffic, cast(dict[str, object], raw))
            self.trace(
                Op.panel.client_traffic, "found",
                panel=panel.name, email=email, uuid=traffic.uuid,
                enable=traffic.enable, up=traffic.up, down=traffic.down,
                total=traffic.total, expiry_time=traffic.expiryTime,
                reset=traffic.reset, last_online=traffic.lastOnline,
            )
            return traffic
        except AppError:
            raise
        except Exception as exc:
            raise PanelUnavailableError(
                f"Panel {panel.name} traffic query failed: {exc}"
            ) from exc

    def _fetch_onlines(self, panel: XUiSession) -> Response | BaseException:
        """POST the onlines route. Exceptions travel back as values."""
        try:
            return panel.post("panel/api/clients/onlines")
        except Exception as exc:
            return exc

    def _classify_onlines(
        self,
        panel: XUiSession,
        response: Response,
        online_users: dict[str, None],
        known: set[str],
    ) -> Literal["ok", "unavailable", "invalid"]:
        """Classify one onlines payload. Accepted emails stay even on a later break."""
        data: dict[str, object] = response.json()
        if response.status_code not in (200, 201) or not data.get("success"):
            self.log.error(
                "Online check failed for panel %s: %s",
                panel.name,
                data.get("msg") or response.status_code,
            )
            return "unavailable"
        raw_online = data.get("obj", [])
        if not isinstance(raw_online, list):
            self.log.error(
                "Online check returned invalid payload for panel %s",
                panel.name,
            )
            return "invalid"
        raw_emails: list[object] = cast(list[object], raw_online)
        for raw_email in raw_emails:
            if not isinstance(raw_email, str):
                self.log.error(
                    "Online check returned a non-string email for panel %s",
                    panel.name,
                )
                return "invalid"
            if raw_email in known:
                # dict, not set: free-threaded sets do not keep first-seen order.
                online_users.setdefault(raw_email, None)
        return "ok"

    @overload
    def get_online_status(
        self,
        new: Literal[False] = False,
        *,
        pool: Executor | None = None,
        fresh: bool = False,
    ) -> OnlineStatus: ...

    @overload
    def get_online_status(
        self,
        new: Literal[True],
        *,
        pool: Executor | None = None,
        fresh: bool = False,
    ) -> OnlineStatus: ...

    @overload
    def get_online_status(
        self,
        new: bool,
        *,
        pool: Executor | None = None,
        fresh: bool = False,
    ) -> OnlineStatus: ...

    def get_online_status(
        self,
        new: bool = False,
        *,
        pool: Executor | None = None,
        fresh: bool = False,
    ) -> OnlineStatus:
        """Get online users and per-panel query health.

        An empty result is valid only when every configured panel reports
        a successful empty response.

        A finished classification is reused for ``_ONLINES_TTL`` seconds.
        ``fresh`` skips that copy and replaces it. A total outage is not
        stored, so the next call tries the panels again.
        """
        if not self.panels:
            panel_health: dict[str, Literal["ok", "unavailable", "invalid"]] = {}
            if new:
                return OnlineStatus({}, panel_health)
            return OnlineStatus([], panel_health)

        snapshot = self._online_snapshot(pool, fresh)
        return self._status_from_snapshot(snapshot, new)

    def _online_snapshot(
        self, pool: Executor | None, fresh: bool,
    ) -> _OnlineSnapshot:
        """One in-flight classification. Waiters share the leader's result.

        The fetch runs outside the condition. Holding it across panel HTTP
        would stall every other online read for the slowest panel, and a
        worker must not take this lock: ``map_panels`` can run on a pool
        thread only as the ``fn``, never as a nested online read.
        """
        with self._onlines_lock:
            if not fresh:
                cached = self._fresh_onlines()
                if cached is not None:
                    self.trace(
                        Op.panel.online_snapshot, "reuse",
                        users=len(cached.users),
                        panel_health=dict(cached.panel_health),
                        fresh=fresh,
                    )
                    return cached
            while self._onlines_loading:
                self.trace(Op.panel.online_snapshot, "waiting", fresh=fresh)
                self._onlines_lock.wait()
                cached = self._fresh_onlines()
                if cached is not None and not fresh:
                    self.trace(
                        Op.panel.online_snapshot, "reuse",
                        users=len(cached.users),
                        panel_health=dict(cached.panel_health),
                        fresh=fresh, waited=True,
                    )
                    return cached
                # A fresh caller does not take the copy a non-fresh leader
                # just stored. It loads after that leader finishes.
            self._onlines_loading = True
        try:
            snapshot = self._load_online_snapshot(pool)
        except BaseException:
            with self._onlines_lock:
                self._onlines_loading = False
                self._onlines_lock.notify_all()
            raise
        with self._onlines_lock:
            self._onlines = snapshot
            self._onlines_loading = False
            self._onlines_lock.notify_all()
        self.trace(
            Op.panel.online_snapshot, "refreshed",
            users=len(snapshot.users),
            panel_health=dict(snapshot.panel_health),
            fresh=fresh,
            pool=type(pool).__name__ if pool is not None else None,
        )
        return snapshot

    def _fresh_onlines(self) -> _OnlineSnapshot | None:
        """Caller holds ``_onlines_lock``."""
        cached = self._onlines
        if cached is None or monotonic() - cached.stored_at >= _ONLINES_TTL:
            return None
        return cached

    def _load_online_snapshot(self, pool: Executor | None) -> _OnlineSnapshot:
        # First-seen order is panel list order, then payload order.
        online_users: dict[str, None] = {}
        panel_health: dict[str, Literal["ok", "unavailable", "invalid"]] = {}
        known = self.db.usernames()

        # Skip dead panels before submit. Workers only fetch; classification
        # and logging stay on the caller so order follows self.panels.
        live = [panel for panel in self.panels if not panel.dead]
        fetched: dict[str, Response | BaseException] = {}
        if live:
            fetched = dict(zip(
                (panel.name for panel in live),
                self.map_panels(live, self._fetch_onlines, pool),
                strict=True,
            ))

        for panel in self.panels:
            if panel.dead:
                panel_health[panel.name] = "unavailable"
                continue
            outcome = fetched[panel.name]
            if isinstance(outcome, BaseException):
                panel_health[panel.name] = "unavailable"
                self.log.error(
                    "Online check failed for panel %s", panel.name, exc_info=outcome
                )
                continue
            try:
                panel_health[panel.name] = self._classify_onlines(
                    panel, outcome, online_users, known
                )
            except Exception as exc:
                panel_health[panel.name] = "unavailable"
                self.log.error(
                    "Online check failed for panel %s", panel.name, exc_info=exc
                )

        if panel_health and all(
            health == "unavailable" for health in panel_health.values()
        ):
            self.trace(
                Op.panel.load_online_snapshot, "unavailable",
                panels=list(panel_health),
                panel_health=panel_health,
                users=len(online_users),
                known=len(known),
                live=[panel.name for panel in live],
            )
            raise PanelUnavailableError("No panel could be queried for online users")
        self.trace(
            Op.panel.load_online_snapshot, "loaded",
            panels=list(panel_health),
            panel_health=panel_health,
            users=len(online_users),
            known=len(known),
            live=[panel.name for panel in live],
            pool=type(pool).__name__ if pool is not None else None,
        )
        return _OnlineSnapshot(online_users, panel_health, monotonic())

    def _status_from_snapshot(self, snapshot: _OnlineSnapshot, new: bool) -> OnlineStatus:
        health = dict(snapshot.panel_health)
        if not new:
            return OnlineStatus(list(snapshot.users), health)
        exts = self.db.username_exts()
        return OnlineStatus(
            {name: exts.get(name) for name in snapshot.users}, health,
        )

    @overload
    def get_online_users(self, new: Literal[False] = False) -> list[str]: ...

    @overload
    def get_online_users(self, new: Literal[True]) -> dict[str, str | None]: ...

    @overload
    def get_online_users(self, new: bool) -> list[str] | dict[str, str | None]: ...

    def get_online_users(self, new: bool = False) -> list[str] | dict[str, str | None]:
        """Compatibility wrapper for callers that do not need panel health."""
        return self.get_online_status(new).users

    def is_online(self, username: str) -> bool:
        """Return True only when the user is online and every panel is healthy.

        No configured panels is an explicit known-empty state rather than an
        availability failure.
        """
        if not self.panels:
            return False
        status = self.get_online_status()
        return username in status.users and any(
            health == "ok" for health in status.panel_health.values()
        )
