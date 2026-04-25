from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from requests import Session

from loggers import Logger
from custom_types import Inbound

from typing import Any
__all__ = ['XUiSession']

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
            ignore_inbounds: A list of inbound IDs to permanently ignore.
            inject_headers: Extra headers merged into every request. 
                Caller-supplied headers take precedence.
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
            self.last_login: datetime | None = None
            self._lock = threading.RLock()
            self._running = threading.Event()
            self._cache_lock = threading.Lock()
            self._cache: list[Inbound] | None = None
            self._cache_time: float = 0
            self._inject_headers = inject_headers or {}

            if nginx_auth:
                self.auth = nginx_auth

            self.login()

    def request(self, *args: Any, **kwargs: Any):
        with self._lock:
            if self._needs_refresh():
                self.login()
        headers = kwargs.get("headers", {})
        kwargs["headers"] = {**self._inject_headers, **headers}
        return super().request(*args, **kwargs)

    def login(self):
        self.log.debug(f"{self.address}:{self.port} > logging into 3x-ui ")
        with self._lock:
            if self.last_login and not self._needs_refresh():
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

                self.last_login = datetime.now()
                self.log.info(f"{self.address}:{self.port} > logged in as {self.username}")

                if not self._running.is_set():
                    self._start_refresh_thread()

            except Exception as e:
                self.log.critical(f"{self.address}:{self.port} > login failed: {str(e)}")
                raise

    def _start_refresh_thread(self):
        self._running.set()
        def refresh_loop():
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

    def _needs_refresh(self):
        if not self.last_login:
            return True
        return (datetime.now() - self.last_login) > timedelta(minutes=self.refresh_interval)

    def get_cache(self) -> list[Inbound] | None:
        """Thread-safe cache read."""
        with self._cache_lock:
            return self._cache

    def set_cache(self, data: list[Inbound]) -> None:
        """Thread-safe cache write."""
        with self._cache_lock:
            self._cache = data
            self._cache_time = time.monotonic()

    def clear_cache(self) -> None:
        """Thread-safe cache invalidation."""
        with self._cache_lock:
            self._cache = None
            self._cache_time = 0

    def close(self):
        self._running.clear()
        super().close()

    def __del__(self):
        self.close()
