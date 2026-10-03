from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from collections.abc import Callable, Sequence
from urllib.parse import quote
from dacite import from_dict
from typing import Literal, cast, overload

from requests import Response

from ..common import BaseService
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

__all__ = ["PanelService"]

_PANEL_POOL_WORKERS = 8
_PANEL_POOL = ThreadPoolExecutor(
    max_workers=_PANEL_POOL_WORKERS, thread_name_prefix="panel"
)

class PanelService(BaseService):
    def map_panels[T](
        self,
        panels: Sequence[XUiSession],
        fn: Callable[[XUiSession], T],
    ) -> list[T]:
        """Run ``fn`` on each panel. Results follow ``panels`` order.

        Wall time is the slowest call, not the sum. An exception is raised
        from the first failing panel in list order and does not cancel the
        others. ``fn`` must not call ``map_panels`` (one shared pool; a
        worker that waits on the same pool deadlocks once it is full).
        Zero or one panel runs on the caller thread.
        """
        if len(panels) <= 1:
            return [fn(panel) for panel in panels]
        futures: list[Future[T]] = [
            _PANEL_POOL.submit(fn, panel) for panel in panels
        ]
        # result() in input order: Executor.map would cancel not-yet-started
        # siblings on the first exception and drop a later panel's work.
        return [future.result() for future in futures]

    def statuses(
        self,
        panels: Sequence[XUiSession] | None = None,
    ) -> list[ServerMetricsResponse | None]:
        """Fan out ``getstatus``. Results align with the input sequence."""
        target = self.panels if panels is None else panels
        return self.map_panels(target, self.getstatus)

    def getstatus(self, panel: XUiSession) -> ServerMetricsResponse | None:
        """Get panel status, or ``None`` when the panel status is unknown."""
        try:
            response = panel.get("panel/api/server/status")
            data: dict[str, object] = response.json()
            if response.status_code != 200:
                message = data.get("msg") or response.status_code
                raise PanelUnavailableError(f"Panel {panel.name} status query failed: {message}")
            return from_dict(ServerMetricsResponse, data)
        except Exception:
            self.log.error("panel status is unknown for %s", panel.name, exc_info=True)
            return None

    def getinbounds(self, panel: XUiSession) -> list[Inbound]:
        """Get inbounds list. Uses cache with TTL, panel.local (almost) skips cache.

        Filters by ``panel.mode`` and ``panel.inbounds_list``: whitelist keeps
        only listed IDs, blacklist drops them.
        """
        ttl = 2 if panel.local else 15  # fast local, slow remote

        cached = panel.fresh_cache(ttl)
        if cached is not None:
            return cached # NOTE: cache stores dataclasses!
        
        try:
            response = panel.get(f"panel/api/inbounds/list")
            data: dict[str, list[dict[str, object]]] = response.json()
            if response.status_code not in (200,) or not data.get("success"):
                raise PanelUnavailableError(
                    f"Panel {panel.name} inbound query failed: "
                    f"{data.get('msg') or response.status_code}"
                )
            raw_inbounds: list[dict[str, object]] = data['obj']
            inbounds = [from_dict(Inbound, i) for i in raw_inbounds]
            listed = set(panel.inbounds_list)
            if panel.mode == "whitelist":
                inbounds = [i for i in inbounds if i.id in listed]
            else:
                inbounds = [i for i in inbounds if i.id not in listed]
            panel.cache = inbounds
            return inbounds
        except AppError:
            raise
        except Exception as exc:
            raise PanelUnavailableError(
                f"Panel {panel.name} inbound query failed: {exc}"
            ) from exc

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
                    return None
                raise PanelRejectedError(
                    f"Panel {panel.name} client query failed: {message}"
                )
            raw: object = data.get("obj")
            if raw is None:
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
            return from_dict(PanelClient, merged)
        except AppError:
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
            return from_dict(ClientListResponse, data).obj
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
                return None
            return from_dict(ClientTraffic, cast(dict[str, object], raw))
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
            if self.db.user_exists(raw_email):
                # dict, not set: free-threaded sets do not keep first-seen order.
                online_users.setdefault(raw_email, None)
        return "ok"

    @overload
    def get_online_status(self, new: Literal[False] = False) -> OnlineStatus: ...

    @overload
    def get_online_status(self, new: Literal[True]) -> OnlineStatus: ...

    @overload
    def get_online_status(self, new: bool) -> OnlineStatus: ...

    def get_online_status(self, new: bool = False) -> OnlineStatus:
        """Get online users and per-panel query health.

        An empty result is valid only when every configured panel reports
        a successful empty response.
        """
        # First-seen order is panel list order, then payload order.
        online_users: dict[str, None] = {}
        panel_health: dict[str, Literal["ok", "unavailable", "invalid"]] = {}

        if not self.panels:
            if new:
                return OnlineStatus({}, panel_health)
            return OnlineStatus([], panel_health)

        # Skip dead panels before submit. Workers only fetch; classification
        # and logging stay on the caller so order follows self.panels.
        live = [panel for panel in self.panels if not panel.dead]
        fetched: dict[str, Response | BaseException] = {}
        if live:
            fetched = dict(zip(
                (panel.name for panel in live),
                self.map_panels(live, self._fetch_onlines),
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
                    panel, outcome, online_users
                )
            except Exception as exc:
                panel_health[panel.name] = "unavailable"
                self.log.error(
                    "Online check failed for panel %s", panel.name, exc_info=exc
                )

        if panel_health and all(
            health == "unavailable" for health in panel_health.values()
        ):
            raise PanelUnavailableError("No panel could be queried for online users")

        users: list[str] | dict[str, str | None]
        if not new:
            users = list(online_users)
        else:
            users = {name: self.db.user_to_ext(name) for name in online_users}
        return OnlineStatus(users, panel_health)

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
