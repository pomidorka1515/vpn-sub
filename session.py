from __future__ import annotations

import threading
import time
import json

from requests import Session, Response, Timeout, ConnectionError, RequestException
from requests.structures import CaseInsensitiveDict
from concurrent.futures import ThreadPoolExecutor, Future
from loggers import Logger
from custom_types import Inbound, JsonValue, RequestKwargs
from errors import XUiSessionError

from typing import Unpack, cast, Any, Mapping

__all__ = ['XUiSession', 'XUiSessionError']

_HEALTH_CHECK_TIMEOUT = 5.0
_LOGIN_TIMEOUT = 10.0
_DEFAULT_REQUEST_TIMEOUT = 5.0
_AUTH_BACKOFF_INITIAL = 30.0
_AUTH_BACKOFF_MAX = 15 * 60.0



### ANY COUNTER: two. ###
# mypy: disable-error-code="override"
# mypy: disable-error-code="redundant-cast"
# pyright: reportIncompatibleMethodOverride=false

class _FakeResponse(Response):
    def __init__(self, json_data: Mapping[str, JsonValue], status_code: int):
        super().__init__()
        self._content = json.dumps(json_data).encode('utf-8')
        self.status_code = status_code
        self.headers = CaseInsensitiveDict({'Content-Type': 'application/json'})
    
class XUiSession(Session):
    """Persistent X-UI Session. Supports basic auth, https, and more.
    Please note that this should only be used in Subscripiton, nowhere else."""
    def __init__(self,
            name: str,
            address: str,
            port: int | str,
            uri: str,
            username: str,
            password: str,
            refresh_interval: int | float = 60,
            https: bool = False,
            nginx_auth: tuple[str, str] | None = None,  # nginx_auth=('user', 'pass')
            ignore_inbounds: tuple[int, ...] = (),  # Can be empty
            inject_headers: Mapping[str, str | bytes] | None = None,
            maximum_concurrent_executors: int = 15,
            health_check_interval: int = 20
    ):
        """
        Args:
            name: The display name for a panel.
            address: Hostname or IP of the panel.
            port: Port of the panel.
            uri: The secret random path (e.g. https://your-panel.com/randompath/panel/api/login)
            username: Username (internal)
            password: Password (internal)
            refresh_interval: Interval in minutes, controls session refresh cycle.
            https: set False for HTTP.
            nginx_auth: External authentication (A.K.A. Basic Auth.). Format: ('username', 'password')
            ignore_inbounds: A tuple of inbound IDs to permanently ignore.
            inject_headers: Extra headers merged into every request. 
                Caller-supplied headers take precedence.
            maximum_concurrent_executors: Maximum amount of asyncronous ThreadPoolExecutor functions
                running at the same time. 
            health_check_interval: Interval in seconds between panel health checks.
        """
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            super().__init__()
            self.username = username
            self.password = password
            self.refresh_interval = refresh_interval
            self.ignore_inbounds = ignore_inbounds or ()
            protocol = "https" if https else "http"
            clean_uri = f"/{uri.strip('/')}/" if uri.strip('/') else "/"
            self.port = str(port)
            self.address = address
            self.name = name
            self.local = self.address in ('localhost', '::1', '127.0.0.1', '0.0.0.0')
            self.base_url = f"{protocol}://{address}:{self.port}{clean_uri}"
            self._login_monotonic: float = 0
            self._login_retry_at: float = 0
            self._login_failures: int = 0
            self._lock = threading.RLock()
            self._running = threading.Event()
            self._refresh_thread: threading.Thread | None = None
            self._cache_lock = threading.Lock()
            self._cache: list[Inbound] | None = None
            self.cache_time: float = 0
            self._inject_headers: Mapping[str, str | bytes] = inject_headers or {}
            
            if maximum_concurrent_executors < 1:
                raise ValueError("maximum_concurrent_executors must be more than 1")
            elif maximum_concurrent_executors < 5:
                self.log.warning("A small limit of thread executors makes asynchronous operations pointless. Consider raising it.")
            
            self._executor = ThreadPoolExecutor(max_workers=maximum_concurrent_executors, thread_name_prefix="3x-ui")

            if nginx_auth:
                self.auth = nginx_auth

            if health_check_interval < 1:
                raise ValueError("health_check_interval must be more than 1")
            elif health_check_interval < 5:
                self.log.warning("A low health_check_interval may cause lag. Proceed with caution.")
            
            self._health_check_interval = health_check_interval
            self._dead: bool = False
            self._health_check_lock = threading.Lock()
            self._health_check_thread = threading.Thread(target=self._health_check, name="3x-ui health check", daemon=True)
            self._health_check_event = threading.Event()

            self.login()

            self._start_health_check_thread()

    @property
    def dead(self) -> bool:
        with self._health_check_lock:
            return self._dead
    
    @dead.setter
    def dead(self, value: bool, /) -> None:
        with self._health_check_lock:
            self._dead = value

    
    def _format_url(self, url: str, /) -> str:
        # Strip /panel/ prefix from base_url since URLs passed are relative to it
        base = self.base_url.rstrip('/')
        panel_prefix = '/panel'
        if base.endswith(panel_prefix):
            base = base[:-len(panel_prefix)]
        if not url.startswith(base):
            return f"{base}/{url.lstrip('/')}"
        return url
    
    def _start_health_check_thread(self) -> None:
        self._health_check_thread.start()

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

    def _perform_health_check(self) -> None:
        try:
            response = super().request(
                "GET",
                self._format_url("panel/api/inbounds/list"),
                headers=dict(self._inject_headers),
                timeout=_HEALTH_CHECK_TIMEOUT,
            )

            if response.status_code in (401, 403):
                with self._lock:
                    retry_backoff_active = time.monotonic() < self._login_retry_at
                if retry_backoff_active:
                    self._mark_dead("authentication retry backoff active")
                    return
                try:
                    self.login()
                except Exception as error:
                    self._mark_dead(f"login failed: {error}")
                    return
                response = super().request(
                    "GET",
                    self._format_url("panel/api/inbounds/list"),
                    headers=dict(self._inject_headers),
                    timeout=_HEALTH_CHECK_TIMEOUT,
                )

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

    def _request_core(
        self,
        method: str,
        url: str,
        **kwargs: Unpack[RequestKwargs],
    ) -> Response:
        """Custom behavior with ParamSpec signature forwarding."""
        if self.dead:
            return _FakeResponse(
                {"success": False, "msg": f"Panel {self.name} is down", "obj": None}, 503
            )

        with self._lock:
            if self._needs_refresh():
                self.login()

    
        url = self._format_url(url)
        kwargs["headers"] = {**cast(Any, kwargs.get("headers", {})), **self._inject_headers}
        kwargs.setdefault("timeout", _DEFAULT_REQUEST_TIMEOUT)
    
        try:
            return super().request(method, url, **cast(Any, kwargs)) # supress protocol mismatches
        except Timeout:
            self._mark_dead(f"timeout of {_DEFAULT_REQUEST_TIMEOUT:.0f} seconds exceeded")
            return _FakeResponse(
                {"success": False, "msg": f"Panel {self.name} is down", "obj": None}, 503
            )
        except ConnectionError as e:
            self._mark_dead(f"connection error: {e}")
            return _FakeResponse(
                {"success": False, "msg": f"Panel {self.name} is down", "obj": None}, 503
            )
        except RequestException as e:
            self._mark_dead(f"request error: {e}")
            return _FakeResponse(
                {"success": False, "msg": f"Panel {self.name} is down", "obj": None}, 503
            )
    
    def request(self, method: str, url: str, **kwargs: Unpack[RequestKwargs]) -> Response:
        return self._request_core(method, url, **kwargs)
    
    def _async_request_wrapper(
        self,
        log: bool,
        method: str,
        url: str,
        **kwargs: Unpack[RequestKwargs],
    ) -> Response:
        """Executed inside the ThreadPoolExecutor to log failures without blocking."""
        url_fixed = self._format_url(url)
        # son im crine why is pyright blind here, dumb asf
        # this is intentional tho
        resp = self._request_core( # pyright: ignore[reportUnknownVariableType]
            method=method,
            url=url_fixed,
            timeout=kwargs.pop('timeout', _DEFAULT_REQUEST_TIMEOUT),
            **kwargs, # type: ignore[misc]
        )
        resp = cast(Response, resp)
        if log:
            if resp.status_code >= 500:
                self.log.error(f"async request failed: HTTP {resp.status_code} on {url_fixed}")
            elif resp.status_code >= 400:
                self.log.warning(f"async request failed: HTTP {resp.status_code} on {url_fixed}")
            try:
                content = resp.json()
                if not content.get('success'):
                    self.log.error(f"async request failed: {content.get('msg')} on {url_fixed}")
            except (json.JSONDecodeError, ValueError):
                pass
    
        return resp
    
    def async_request(
        self,
        method: str,
        url: str,
        log: bool,
        **kwargs: Unpack[RequestKwargs],
    ) -> Future[Response]:
        return self._executor.submit(self._async_request_wrapper, log, method, url, **kwargs)
    
    def post_async(self, url: str, log: bool = False, **kwargs: Unpack[RequestKwargs]) -> Future[Response]:
        return self.async_request(method='POST', url=url, log=log, **kwargs)
    
    def get_async(self, url: str, log: bool = False, **kwargs: Unpack[RequestKwargs]) -> Future[Response]:
        return self.async_request(method='GET', url=url, log=log, **kwargs)

    def login(self) -> None:
        self.log.debug(f"{self.address}:{self.port} > logging into 3x-ui")
        with self._lock:
            if self._login_monotonic and not self._needs_refresh():
                return
            try:
                login_url = self._format_url("login")
                login_data = {"username": self.username, "password": self.password}
                headers = {
                    **dict(self._inject_headers),
                    "Content-Type": "application/json",
                }
                response = super().request(
                    "POST",
                    login_url,
                    json=login_data,
                    headers=headers,
                    timeout=_LOGIN_TIMEOUT
                )

                if response.status_code != 200:
                    raise XUiSessionError(f"HTTP {response.status_code}")

                json_res = response.json()
                if not json_res.get("success"):
                    raise XUiSessionError(f"{json_res.get('msg')}")

                self._login_monotonic = time.monotonic()
                self._login_failures = 0
                self._login_retry_at = 0
                self.log.info(f"{self.address}:{self.port} > logged in as {self.username}")

                if not self._running.is_set():
                    self._start_refresh_thread()

            except Exception as e:
                if isinstance(e, XUiSessionError):
                    self._login_failures += 1
                    delay = min(_AUTH_BACKOFF_INITIAL * (2 ** (self._login_failures - 1)), _AUTH_BACKOFF_MAX)
                    self._login_retry_at = time.monotonic() + delay
                if self.dead:
                    self.log.debug(f"{self.address}:{self.port} > login failed", exc_info=True)
                else:
                    self.log.critical(f"{self.address}:{self.port} > login failed", exc_info=True)
                raise
                
    def _start_refresh_thread(self) -> None:
        self._running.set()
        def refresh_loop() -> None:
            while self._running.wait(60):
                if self._needs_refresh():
                    try:
                        self.log.info(f"{self.address}:{self.port} > refreshing session")
                        self.login()
                    except Exception:
                        pass
        thread = threading.Thread(target=refresh_loop, daemon=True, name="3x-ui")
        self._refresh_thread = thread
        thread.start()

    def _needs_refresh(self) -> bool:
        with self._lock:
            if not self._login_monotonic:
                return True
            return (time.monotonic() - self._login_monotonic) > (self.refresh_interval * 60)

    @property
    def cache(self) -> list[Inbound] | None:
        with self._cache_lock:
            return self._cache
            
    @cache.setter
    def cache(self, value: list[Inbound], /) -> None:
        with self._cache_lock:
            self._cache = value
            self.cache_time = time.monotonic()
    
    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache = None
            self.cache_time = 0

    def close(self) -> None:
        self._running.clear()
        self._health_check_event.set()
        if self._refresh_thread is not None and self._refresh_thread.is_alive():
            self._refresh_thread.join(timeout=2)
        if self._health_check_thread.is_alive():
            self._health_check_thread.join(timeout=2)
        self._executor.shutdown(wait=True)
        super().close()

    
