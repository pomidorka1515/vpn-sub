"""3x-ui panel client: Bearer auth, health checks, and two generation caches."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from typing import Literal, Unpack

from requests import ConnectionError, RequestException, Response, Session, Timeout  # noqa: A004

from custom_types import Inbound, PanelClient, RequestKwargs
from loggers import Logger, color_status

from .cache import GenerationCache
from .stamp import client_stamp_path as default_client_stamp_path
from .stamp import inbound_stamp_path as default_inbound_stamp_path
from .transport import (
    FakeResponse,
    RequestsPanelTransport,
    XUiPanelTransport,
    new_session,
)

__all__ = ["XUiSession"]

_HEALTH_CHECK_TIMEOUT = 5.0
_DEFAULT_REQUEST_TIMEOUT = 5.0


class XUiSession:
    """
    3x-ui panel client with Bearer-token auth, health checks, and two caches.

    Authentication is a static admin-scoped API token sent as
    ``Authorization: Bearer <token>`` on every request. There is no login and no
    session refresh. A 401/403 from the panel marks it dead with a distinct
    "token rejected" reason -- the token is bad or revoked and must be reissued
    in config; no automatic recovery is attempted.

    Thread-safety and lifecycle:
      - ``dead`` is protected by an internal lock.
      - Cache reads return the stored list unchanged; callers must not mutate it.
      - A stamp file in the runtime dir is the cross-process generation.
        ``clear_cache`` bumps its mtime before dropping the local list, so a
        failed bump cannot leave other workers serving a stale list. The list
        itself stays in memory. A missing stamp is not generation 0.
      - The client map has its own stamp, lock, and clock. Dropping inbounds
        must not drop clients, and the reverse. Client rows are never written
        to the stamp file.
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
        verbose: bool = False,
        transport: XUiPanelTransport | None = None,
        session: Session | None = None,
        clock: Callable[[], float] = time.monotonic,
        stamp_path: str | None = None,
        stamp_dir: str | None = None,
        client_stamp_path: str | None = None,
    ) -> None:
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
            verbose: When True, log every completed panel HTTP request at DEBUG.
                The line matches the gunicorn access format, including status
                color. Dead-panel short-circuits and transport failures are not
                requests and are not logged this way. The secret URI prefix and
                query string are omitted.
            transport: Replacement HTTP transport. If omitted, a transport is built around session.
            session: Session used by the default transport. Defaults to a new ``requests.Session``.
            clock: Monotonic clock used for cache timing.
            stamp_path: Generation file shared by every worker for this panel.
                Defaults to a file under ``stamp_dir``. Tests pass a temp path.
            stamp_dir: Directory for the default generation file. Defaults to
                the service runtime dir (``DIR_RUNTIME``, else ``<DIR_DATA>/run``).
                Ignored when ``stamp_path`` / ``client_stamp_path`` is set for
                that cache. Must be local: ``flock`` is
                not reliable on NFS, and the stamp is only a cross-process mtime.
            client_stamp_path: Generation file for the client map. Defaults to
                a sibling of the inbound stamp. Tests pass a temp path. Must
                not be the inbound stamp: the two caches invalidate apart.
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
            self.local = self.address in ("localhost", "::1", "127.0.0.1", "0.0.0.0") # noqa: S104
            self.base_url = f"{protocol}://{address}:{self.port}{clean_uri}"

            resolved_stamp = stamp_path if stamp_path is not None else default_inbound_stamp_path(
                name, self.base_url, directory=stamp_dir,
            )
            resolved_client_stamp = client_stamp_path
            if resolved_client_stamp is None:
                resolved_client_stamp = default_client_stamp_path(
                    name, self.base_url, directory=stamp_dir,
                )
            if os.path.abspath(resolved_client_stamp) == os.path.abspath(resolved_stamp):
                raise ValueError("client stamp must not be the inbound stamp")
            self._inbounds: GenerationCache[list[Inbound]] = GenerationCache(resolved_stamp, clock)
            self._clients: GenerationCache[dict[str, PanelClient]] = GenerationCache(
                resolved_client_stamp, clock,
            )
            self._inject_headers: Mapping[str, str | bytes] = inject_headers or {}
            self.verbose = verbose

            self._session: Session | None = None
            self._owns_session = session is None and transport is None
            if transport is None:
                self._session = session if session is not None else new_session()
                transport = RequestsPanelTransport(self._session, nginx_auth)
            elif session is not None:
                raise ValueError("pass either transport or session, not both")
            self._transport = transport

            if health_check_interval < 1:
                raise ValueError("health_check_interval must be more than 1")
            if health_check_interval < 5:
                self.log.warning("a low health_check_interval may cause lag. proceed with caution.")

            self._health_check_interval = health_check_interval
            self._dead = False
            self._health_check_lock = threading.Lock()
            self._health_check_thread = threading.Thread(
                target=self._health_check,
                name="XUi healthcheck",  # NOTE: intended, <= 15 chars
                daemon=True,
            )
            self._health_check_event = threading.Event()

            self._health_check_thread.start()

    @staticmethod
    def _new_session() -> Session:
        """Session whose pool can hold one connection per panel worker."""
        return new_session()

    @property
    def dead(self) -> bool:
        with self._health_check_lock:
            return self._dead

    @dead.setter
    def dead(self, value: bool, /) -> None:
        with self._health_check_lock:
            self._dead = value

    def _format_url(self, url: str, /) -> str:
        base = self.base_url.rstrip("/")
        panel_prefix = "/panel"
        base = base.removesuffix(panel_prefix)
        if not url.startswith(base):
            return f"{base}/{url.lstrip('/')}"
        return url

    def _request_target(self, url: str, /) -> str:
        """Panel route only. The secret URI prefix and query string stay out."""
        path = url.split("?", 1)[0]
        base = self.base_url.rstrip("/")
        panel_prefix = "/panel"
        base = base.removesuffix(panel_prefix)
        path = path.removeprefix(base)
        if not path.startswith("/"):
            path = f"/{path}"
        return path or "/"

    def _log_request(self, method: str, url: str, response: Response) -> None:
        if not self.verbose:
            return
        status = str(response.status_code)
        size = len(response.content)
        raw = getattr(response, "raw", None)
        version = getattr(raw, "version", None)
        # urllib3 reports 10 for HTTP/1.0 and 11 for HTTP/1.1.
        protocol = "HTTP/1.1"
        if version == 10:
            protocol = "HTTP/1.0"
        target = self._request_target(url)
        self.log.debug(
            f'"{method} {target} {protocol}" {color_status(status)} {size}b'
        )

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
        except Timeout:
            self._mark_dead(f"timeout of {_HEALTH_CHECK_TIMEOUT:.0f} seconds exceeded")
            return
        except ConnectionError as error:
            self._mark_dead(f"connection error: {error}")
            return
        except RequestException as error:
            self._mark_dead(f"request error: {error}")
            return

        self._log_request("GET", "panel/api/inbounds/list", response)
        if response.status_code in (401, 403):
            self._mark_dead(f"token rejected (HTTP {response.status_code})")
            return

        if response.status_code != 200:
            self._mark_dead(f"HTTP {response.status_code}")
            return

        content = response.json()
        if not content.get("success"):
            self._mark_dead(f"panel returned message: {content.get('msg')}")
            return

        self._mark_alive()

    def _health_check(self) -> None:
        while not self._health_check_event.wait(self._health_check_interval):
            self._perform_health_check()

    def _down_response(self, reason: str) -> Response:
        return FakeResponse(
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
            return self._down_response("unavailable")

        request_url = self._format_url(url)
        kwargs["headers"] = {
            **(kwargs.get("headers") or {}),
            **self._inject_headers,
            # set last: the panel Authorization must not be overridable by accident
            **self._auth_header(),
        }
        kwargs.setdefault("timeout", _DEFAULT_REQUEST_TIMEOUT)

        try:
            response = self._transport.request(method, request_url, **kwargs)
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
        self._log_request(method, request_url, response)
        return response

    def get(self, url: str, **kwargs: Unpack[RequestKwargs]) -> Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Unpack[RequestKwargs]) -> Response:
        return self.request("POST", url, **kwargs)

    @property
    def _stamp_path(self) -> str:
        return self._inbounds.path

    @_stamp_path.setter
    def _stamp_path(self, value: str) -> None:
        self._inbounds.path = value

    @property
    def _cache_stamp(self) -> int | None:
        return self._inbounds.stamp

    @_cache_stamp.setter
    def _cache_stamp(self, value: int | None) -> None:
        self._inbounds.stamp = value

    @property
    def _client_stamp_path(self) -> str:
        return self._clients.path

    @_client_stamp_path.setter
    def _client_stamp_path(self, value: str) -> None:
        self._clients.path = value

    @property
    def _clients_stamp(self) -> int | None:
        return self._clients.stamp

    @_clients_stamp.setter
    def _clients_stamp(self, value: int | None) -> None:
        self._clients.stamp = value

    @property
    def cache(self) -> list[Inbound] | None:
        return self._inbounds.get()

    @cache.setter
    def cache(self, value: list[Inbound], /) -> None:
        self._inbounds.set(value)

    @property
    def cache_time(self) -> float:
        return self._inbounds.time

    @cache_time.setter
    def cache_time(self, value: float, /) -> None:
        self._inbounds.time = value

    @property
    def cache_age(self) -> float:
        return self._inbounds.age()

    @property
    def cache_current(self) -> bool:
        return self._inbounds.current()

    def fresh_cache(self, ttl: float) -> list[Inbound] | None:
        return self._inbounds.fresh(ttl)

    def clear_cache(self) -> None:
        self._inbounds.clear()

    @property
    def clients_cache(self) -> dict[str, PanelClient] | None:
        return self._clients.get()

    @clients_cache.setter
    def clients_cache(self, value: dict[str, PanelClient], /) -> None:
        self._clients.set(value)

    def fresh_clients(self, ttl: float) -> dict[str, PanelClient] | None:
        """Client map when its own stamp still agrees. An inbound clear does not expire it."""
        return self._clients.fresh(ttl)

    def clear_clients(self) -> None:
        """Drop the client map only. Does not touch the inbound cache."""
        self._clients.clear()

    def close(self) -> None:
        self._health_check_event.set()
        if self._health_check_thread is not threading.current_thread():
            self._health_check_thread.join(timeout=2)
        if self._owns_session and self._session is not None:
            self._session.close()
