"""HTTP transport for a 3x-ui panel.

The default transport wraps a ``requests.Session`` it does not own. Callers
that share a session remain responsible for its lifecycle and pool limits.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Protocol, Unpack, cast

from requests import Response, Session
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict

from config import JsonValue
from custom_types import RequestKwargs
from errors import XUiSessionError

__all__ = ["XUiPanelTransport", "RequestsPanelTransport", "FakeResponse", "SESSION_POOL_MAXSIZE"]

# panel-req (4) + panel-bg (4). urllib3's default of 10 discards a
# connection once both sides are in flight against the same panel.
SESSION_POOL_MAXSIZE = 8


class FakeResponse(Response):
    def __init__(self, json_data: Mapping[str, JsonValue], status_code: int):
        super().__init__()
        self._content = json.dumps(json_data).encode("utf-8")
        self.status_code = status_code
        self.headers = CaseInsensitiveDict({"Content-Type": "application/json"})


class XUiPanelTransport(Protocol):
    """Transport used to communicate with a 3x-ui panel.

    Implementations must support concurrent calls from client-managed threads.
    """

    def request(
        self,
        method: str,
        url: str,
        **kwargs: Unpack[RequestKwargs],
    ) -> Response:
        """Send a request and return the raw panel response."""
        ...


class RequestsPanelTransport:
    """Transport around a wrapped ``requests.Session``.

    The session is not modified or closed by this transport. ``requests.Session``
    can generally be used concurrently for request submission, but callers sharing
    a session remain responsible for its lifecycle and connection-pool limits.
    """

    def __init__(self, session: Session, auth: tuple[str, str] | None = None):
        self._session: Session = session
        self._auth: tuple[str, str] | None = auth

    def request(
        self,
        method: str,
        url: str,
        **kwargs: Unpack[RequestKwargs],
    ) -> Response:
        if self._auth is not None:
            kwargs.setdefault("auth", self._auth)
        headers = cast(Mapping[str, str | bytes | None] | None, kwargs.get("headers"))
        if (
            kwargs.get("auth") is not None
            and headers is not None
            and any(str(key).lower() == "authorization" for key in headers.keys())
        ):
            raise XUiSessionError(
                "refusing to send basic auth alongside an Authorization header: "
                "requests would silently overwrite the header"
            )
        return self._session.request(method, url, **cast(Any, kwargs))


def new_session() -> Session:
    """Session whose pool can hold one connection per panel worker.

    Both executors can hit the same panel at once. ``pool_maxsize`` is
    their sum so urllib3 does not discard a live connection to make room.
    """
    session = Session()
    adapter = HTTPAdapter(
        pool_connections=SESSION_POOL_MAXSIZE,
        pool_maxsize=SESSION_POOL_MAXSIZE,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
