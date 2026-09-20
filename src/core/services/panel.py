import time
from dacite import from_dict

from ..common import BaseService
from session import XUiSession
from custom_types import ServerMetricsResponse, Inbound
from errors import PanelUnavailableError, AppError

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
        """Get inbounds list. Uses cache with TTL, panel.local (almost) skips cache."""
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
            if panel.ignore_inbounds:
                inbounds = [i for i in inbounds if i.id not in panel.ignore_inbounds]
            panel.cache = inbounds
            return inbounds
        except AppError:
            raise
        except Exception as exc:
            raise PanelUnavailableError(
                f"Panel {panel.name} inbound query failed: {exc}"
            ) from exc

    def get_emails(self, username: str, panel: XUiSession) -> dict[str, str]:
        """Get the panel emails. {'inboundId': 'actual_panel_email', ...}"""
        inbounds = self.getinbounds(panel=panel)
        emails: dict[str, str] = {}
        for i in inbounds:
            for r in i.clientStats:
                actual_email = r.email
                parts = actual_email.rsplit('-', 1)
                parsed_username = parts[0] if len(parts) == 2 else actual_email
                if parsed_username == username:
                    emails[str(i.id)] = actual_email
        return emails