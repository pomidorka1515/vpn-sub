from __future__ import annotations

import threading
import time
import json

from requests import Session, Response, Timeout
from requests.structures import CaseInsensitiveDict
from concurrent.futures import ThreadPoolExecutor, Future
from loggers import Logger
from custom_types import Inbound

from typing import Any

__all__ = ['XUiSession']

class _FakeResponse(Response):
    def __init__(self, json_data: dict[str, object], status_code: int):
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
            inject_headers: dict[str, Any] | None = None,
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
            self._lock = threading.RLock()
            self._running = threading.Event()
            self._cache_lock = threading.Lock()
            self._cache: list[Inbound] | None = None
            self.cache_time: float = 0
            self._inject_headers: dict[str, Any] = inject_headers or {}
            
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

    @property
    def dead(self) -> bool:
        with self._health_check_lock:
            return self._dead
    
    @dead.setter
    def dead(self, value: bool, /) -> None:
        with self._health_check_lock:
            self._dead = value
    
    def _start_health_check_thread(self) -> None:
        self._health_check_thread.start()
    
    def _health_check(self) -> None:
        while not self._health_check_event.wait(self._health_check_interval):
            try:
                resp = super().request("GET", f"{self.base_url}panel/api/inbounds/list")
                if resp.status_code not in [200]:
                    if resp.status_code in [500, 502]:
                        self.dead = True # NOTE: its probably dead
                    else:
                        self.log.error(f"panel {self.name}: HTTP code {resp.status_code}")
                    continue
                content = resp.json()
                if not content.get('success'): # bool
                    self.log.error(f"panel {self.name}: returned message: {content.get('msg')}")
                    self.dead = True
                    continue
                self.dead = False
            except Timeout:
                self.log.error(f"panel {self.name}: timeout of 5 seconds exceeded, panel is down")
                self.dead = True
            except Exception:
                self.log.error(f"panel {self.name}: unknown exception. re-raising")
                self.dead = True
                raise

    def request(self, *args: Any, **kwargs: Any) -> Response:
        with self._lock:
            if self._needs_refresh():
                self.login()
        if self.dead:
            return _FakeResponse({"success": False, "msg": f"Panel {self.name} is down", "obj": None}, 503)
    
        headers = kwargs.get("headers", {})
        kwargs["headers"] = {**self._inject_headers, **headers}
        return super().request(*args, **kwargs)

    def async_request(self, *args: Any, **kwargs: Any) -> Future[Response]:
        return self._executor.submit(self.request, *args, **kwargs)

    def post_async(self, url: str, **kwargs: Any) -> Future[Response]:
        return self.async_request('POST', url, **kwargs)

    def get_async(self, url: str, **kwargs: Any) -> Future[Response]:
        return self.async_request('GET', url, **kwargs)

    def login(self) -> None:
        self.log.debug(f"{self.address}:{self.port} > logging into 3x-ui ")
        with self._lock:
            if self._login_monotonic and not self._needs_refresh():
                return
            try:
                login_url = f"{self.base_url}login"
                login_data = {"username": self.username, "password": self.password}
                response = super().request(
                    "POST",
                    login_url,
                    json=login_data,
                    headers={"Content-Type": "application/json"},
                    timeout=10
                )

                if response.status_code != 200:
                    raise Exception(f"HTTP {response.status_code}")

                json_res = response.json()
                if not json_res.get("success"):
                    raise Exception(f"{json_res.get('msg')}")

                self._login_monotonic = time.monotonic()
                self.log.info(f"{self.address}:{self.port} > logged in as {self.username}")

                if not self._running.is_set():
                    self._start_refresh_thread()

            except Exception as e:
                self.log.critical(f"{self.address}:{self.port} > login failed: {str(e)}")
                raise
                
    def _start_refresh_thread(self) -> None:
        self._running.set()
        def refresh_loop() -> None:
            while self._running.is_set():
                time.sleep(60)
                if self._needs_refresh():
                    try:
                        self.log.info(f"{self.address}:{self.port} > refreshing session")
                        self.login()
                    except Exception:
                        pass
        thread = threading.Thread(target=refresh_loop, daemon=True, name="3x-ui")
        thread.start()

    def _needs_refresh(self) -> bool:
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
            self._cache_time = 0

    def close(self) -> None:
        self._running.clear()
        self._health_check_event.set()
        if self._health_check_thread.is_alive():
            self._health_check_thread.join(timeout=2)
        self._executor.shutdown()
        super().close()

    
