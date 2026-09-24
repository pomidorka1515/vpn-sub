import time
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

class PanelService(BaseService):
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
        now = time.time()
        ttl = 2 if panel.local else 15  # fast local, slow remote

        cached = panel.cache
        if cached is not None and now - panel.cache_time < ttl:
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

        Not-found contract: ``success: true`` with a ``null`` ``obj`` means
        the client is unknown. A non-200 status (404 included) is an
        availability error — a missing route must not look like an absent
        client. ``success: false`` is a rejection and carries the panel
        ``msg``.
        """
        url = f"panel/api/clients/get/{quote(email, safe='')}"
        try:
            response = panel.get(url)
            if response.status_code != 200:
                raise self._status_error(panel, "client query", response)
            data: dict[str, object] = response.json()
            if not data.get("success"):
                raise PanelRejectedError(
                    f"Panel {panel.name} client query failed: "
                    f"{data.get('msg') or response.status_code}"
                )
            raw: object = data.get("obj")
            if raw is None:
                return None
            return from_dict(PanelClient, cast(dict[str, object], raw))
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
        online_users: set[str] = set()
        panel_health: dict[str, Literal["ok", "unavailable", "invalid"]] = {}

        if not self.panels:
            if new:
                return OnlineStatus({}, panel_health)
            return OnlineStatus([], panel_health)

        for panel in self.panels:
            if panel.dead:
                panel_health[panel.name] = "unavailable"
                continue
            try:
                response = panel.post("panel/api/clients/onlines")
                data: dict[str, object] = response.json()
                if response.status_code not in (200, 201) or not data.get('success'):
                    panel_health[panel.name] = "unavailable"
                    self.log.error(
                        "Online check failed for panel %s: %s",
                        panel.name,
                        data.get('msg') or response.status_code,
                    )
                    continue
                raw_online = data.get('obj', [])
                if not isinstance(raw_online, list):
                    panel_health[panel.name] = "invalid"
                    self.log.error(
                        "Online check returned invalid payload for panel %s",
                        panel.name,
                    )
                    continue
                raw_emails: list[object] = cast(list[object], raw_online)
                for raw_email in raw_emails:
                    if not isinstance(raw_email, str):
                        panel_health[panel.name] = "invalid"
                        self.log.error(
                            "Online check returned a non-string email for panel %s",
                            panel.name,
                        )
                        break
                    if self.db.user_exists(raw_email):
                        online_users.add(raw_email)
                else:
                    panel_health[panel.name] = "ok"
            except Exception as exc:
                panel_health[panel.name] = "unavailable"
                self.log.error("Online check failed for panel %s", panel.name, exc_info=exc)

        if all(health == "unavailable" for health in panel_health.values()):
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
