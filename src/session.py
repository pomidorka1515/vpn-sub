from __future__ import annotations

import threading
import time
import json

from requests import Session, Response, Timeout, ConnectionError, RequestException
from requests.structures import CaseInsensitiveDict
from loggers import Logger
from custom_types import Inbound, RequestKwargs
from config import JsonValue
from errors import XUiSessionError

from collections.abc import Callable
from typing import Unpack, cast, Any, Mapping, Protocol, Literal

__all__ = ['XUiSession', 'XUiPanelTransport', 'RequestsPanelTransport', 'XUiSessionError']

_HEALTH_CHECK_TIMEOUT = 5.0
_DEFAULT_REQUEST_TIMEOUT = 5.0



class _FakeResponse(Response):
    def __init__(self, json_data: Mapping[str, JsonValue], status_code: int):
        super().__init__()
        self._content = json.dumps(json_data).encode('utf-8')
        self.status_code = status_code
        self.headers = CaseInsensitiveDict({'Content-Type': 'application/json'})


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
            kwargs.setdefault('auth', self._auth)
        return self._session.request(method, url, **cast(Any, kwargs))


class XUiSession:
    """
    3x-ui panel client with Bearer-token auth, health checks, and inbound caching.

    Authentication is a static admin-scoped API token sent as
    ``Authorization: Bearer <token>`` on every request. There is no login and no
    session refresh. A 401/403 from the panel marks it dead with a distinct
    "token rejected" reason -- the token is bad or revoked and must be reissued
    in config; no automatic recovery is attempted.

    Thread-safety and lifecycle:
      - ``dead`` is protected by an internal lock.
      - Cache reads return the stored list unchanged; callers must not mutate it.
      - ``close`` stops the client-managed health-check thread and closes the
        default session. If a transport or session is injected, its lifecycle
        remains the injector's responsibility. Do not issue requests while or
        after ``close`` is running.
      - Transport implementations must be safe for concurrent request submissions.
    """

    def __init__(
        self,
        *,
        name: str,
        address: str,
        port: int | str,
        uri: str,
        api_token: str,
        https: bool = False,
        nginx_auth: tuple[str, str] | None = None,
        inbounds_list: tuple[int, ...] = (),
        mode: Literal["whitelist", "blacklist"] = "blacklist",
        inject_headers: Mapping[str, str | bytes] | None = None,
        health_check_interval: int = 20,
        transport: XUiPanelTransport | None = None,
        session: Session | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        """
        Initialize the panel client.

        Args:
            name: The display name for a panel.
            address: Hostname or IP of the panel.
            port: Port of the panel.
            uri: The secret random path (e.g. your-panel.com/randompath/panel/api/...).
            api_token: Admin-scoped 3x-ui API token (Settings -> Security -> API Token).
            https: Set False for HTTP.
            nginx_auth: External authentication (A.K.A. Basic Auth.), format ('username', 'password').
            inbounds_list: Inbound IDs used by ``mode``.
            mode: ``whitelist`` keeps only IDs in ``inbounds_list``;
                ``blacklist`` drops those IDs. An empty list keeps no inbounds
                in whitelist mode and all inbounds in blacklist mode.
            inject_headers: Extra headers merged into every request.
                Caller-supplied headers take precedence (except Authorization).
            health_check_interval: Interval in seconds between panel health checks.
            transport: Replacement HTTP transport. If omitted, a transport is built around session.
            session: Session used by the default transport. Defaults to a new ``requests.Session``.
            clock: Monotonic clock used for cache timing.
        """
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            if not api_token:
                raise ValueError("api_token must be a non-empty API token")
            self._api_token = api_token
            if mode not in ("whitelist", "blacklist"):
                raise ValueError("mode must be 'whitelist' or 'blacklist'")
            self.inbounds_list = inbounds_list or ()
            self.mode: Literal["whitelist", "blacklist"] = mode
            protocol = "https" if https else "http"
            clean_uri = f"/{uri.strip('/')}/" if uri.strip('/') else "/"
            self.port = str(port)
            self.address = address
            self.name = name
            self.local = self.address in ('localhost', '::1', '127.0.0.1', '0.0.0.0')
            self.base_url = f"{protocol}://{address}:{self.port}{clean_uri}"

            self._clock = clock

            self._cache_lock = threading.Lock()
            self._cache: list[Inbound] | None = None
            self.cache_time: float = 0
            self._inject_headers: Mapping[str, str | bytes] = inject_headers or {}

            self._session: Session | None = None
            self._owns_session = session is None and transport is None
            if transport is None:
                self._session = session if session is not None else Session()
                transport = RequestsPanelTransport(self._session, nginx_auth)
            elif session is not None:
                raise ValueError("pass either transport or session, not both")
            self._transport = transport

            if health_check_interval < 1:
                raise ValueError("health_check_interval must be more than 1")
            elif health_check_interval < 5:
                self.log.warning("a low health_check_interval may cause lag. proceed with caution.")

            self._health_check_interval = health_check_interval
            self._dead = False
            self._health_check_lock = threading.Lock()
            self._health_check_thread = threading.Thread(
                target=self._health_check,
                name="XUi healthcheck", # NOTE: intended, <= 15 chars
                daemon=True,
            )
            self._health_check_event = threading.Event()

            self._health_check_thread.start()

    @property
    def dead(self) -> bool:
        with self._health_check_lock:
            return self._dead

    @dead.setter
    def dead(self, value: bool, /) -> None:
        with self._health_check_lock:
            self._dead = value

    def _format_url(self, url: str, /) -> str:
        base = self.base_url.rstrip('/')
        panel_prefix = '/panel'
        if base.endswith(panel_prefix):
            base = base[:-len(panel_prefix)]
        if not url.startswith(base):
            return f"{base}/{url.lstrip('/')}"
        return url

    def _mark_dead(self, reason: str) -> None:
        with self._health_check_lock:
            was_alive = not self._dead
            self._dead = True

        if was_alive:
            self.log.warning(f"panel {self.name}: marked down: {reason}")
        else:
            self.log.debug(f"panel {self.name}: still down: {reason}")

    def _mark_alive(self) -> None:
        with self._health_check_lock:
            was_dead = self._dead
            self._dead = False

        if was_dead:
            self.log.info(f"panel {self.name}: recovered")

    def _auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_token}"}

    def _health_request(self) -> Response:
        return self._transport.request(
            "GET",
            self._format_url("panel/api/inbounds/list"),
            headers={**dict(self._inject_headers), **self._auth_header()},
            timeout=_HEALTH_CHECK_TIMEOUT,
        )

    def _perform_health_check(self) -> None:
        try:
            response = self._health_request()
            if response.status_code in (401, 403):
                self._mark_dead(f"token rejected (HTTP {response.status_code})")
                return

            if response.status_code != 200:
                self._mark_dead(f"HTTP {response.status_code}")
                return

            content = response.json()
            if not content.get('success'):
                self._mark_dead(f"panel returned message: {content.get('msg')}")
                return

            self._mark_alive()
        except Timeout:
            self._mark_dead(f"timeout of {_HEALTH_CHECK_TIMEOUT:.0f} seconds exceeded")
        except ConnectionError as error:
            self._mark_dead(f"connection error: {error}")
        except RequestException as error:
            self._mark_dead(f"request error: {error}")

    def _health_check(self) -> None:
        while not self._health_check_event.wait(self._health_check_interval):
            self._perform_health_check()

    def _down_response(self, reason: str) -> Response:
        return _FakeResponse(
            {"success": False, "msg": f"Panel {self.name} is down: {reason}", "obj": None},
            503,
        )

    def request(
        self,
        method: str,
        url: str,
        **kwargs: Unpack[RequestKwargs],
    ) -> Response:
        if self.dead:
            return self._down_response('unavailable')

        request_url = self._format_url(url)
        kwargs['headers'] = {
            **cast(Any, kwargs.get('headers', {})),
            **self._inject_headers,
            # set last: the panel Authorization must not be overridable by accident
            **self._auth_header(),
        }
        kwargs.setdefault('timeout', _DEFAULT_REQUEST_TIMEOUT)

        try:
            return self._transport.request(method, request_url, **cast(Any, kwargs))
        except Timeout:
            reason = f"timeout of {_DEFAULT_REQUEST_TIMEOUT:.0f} seconds exceeded"
            self._mark_dead(reason)
            return self._down_response(reason)
        except ConnectionError as error:
            reason = f"connection error: {error}"
            self._mark_dead(reason)
            return self._down_response(reason)
        except RequestException as error:
            reason = f"request error: {error}"
            self._mark_dead(reason)
            return self._down_response(reason)

    def get(self, url: str, **kwargs: Unpack[RequestKwargs]) -> Response:
        return self.request('GET', url, **kwargs)

    def post(self, url: str, **kwargs: Unpack[RequestKwargs]) -> Response:
        return self.request('POST', url, **kwargs)

    @property
    def cache(self) -> list[Inbound] | None:
        with self._cache_lock:
            return self._cache

    @cache.setter
    def cache(self, value: list[Inbound], /) -> None:
        with self._cache_lock:
            self._cache = value
            self.cache_time = self._clock()

    @property
    def cache_age(self) -> float:
        """Seconds since the inbound cache was last populated (own clock domain)."""
        with self._cache_lock:
            return self._clock() - self.cache_time

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache = None
            self.cache_time = 0

    def close(self) -> None:
        self._health_check_event.set()
        if self._health_check_thread is not threading.current_thread():
            self._health_check_thread.join(timeout=2)
        if self._owns_session and self._session is not None:
            self._session.close()
