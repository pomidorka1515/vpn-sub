from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from requests import Session

from loggers import Logger

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
                 nginx_auth: tuple | None = None,  # nginx_auth=('user', 'pass')
                 ignore_inbounds: list[int] | None = None
                ):
        """
        Args:
            name: The display name for a panel.
            address: Hostname or IP of the panel.
            port: Port of the panel.
            uri: The secret random path (e.g. https://your-panel.com/randompath/panel/api/login)
            username: Username (internal)
            password: Password (internal)
            refresh_interval: Interval in hours, controls login refresh system.
            https: set False for HTTP.
            nginx_auth: External authentication (A.K.A. Basic Auth.). Format: ('username', 'password')
            ignore_inbounds: A list of inbound IDs to permanently ignore.
        """
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            super().__init__()
            self.username = username
            self.password = password
            self.refresh_interval = refresh_interval
            self.ignore_inbounds = ignore_inbounds if ignore_inbounds is not None else []
            protocol = "https" if https else "http"
            clean_uri = f"/{uri.strip('/')}/" if uri.strip('/') else "/"
            self.port = str(port)
            self.address = address
            self.name = name
            self.local = self.address in ('localhost', '::1', '127.0.0.1')
            self.base_url = f"{protocol}://{address}:{self.port}{clean_uri}"
            self.last_login = None
            self._lock = threading.Lock()
            self._running = False
            self._cache_lock = threading.Lock()
            self._cache: list[dict] | None = None
            self._cache_time: float = 0
    
            if nginx_auth:
                self.auth = nginx_auth
            
            self.login()

    def request(self, *args, **kwargs):
        if self._needs_refresh():
            self.login()
        return super().request(*args, **kwargs)

    def login(self):
        self.log.debug(f"{self.address}:{self.port} > logging into 3x-ui ")
        with self._lock:
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

                if not self._running:
                    self._start_refresh_thread()

            except Exception as e:
                self.log.critical(f"{self.address}:{self.port} > login failed: {str(e)}")
                raise

    def _start_refresh_thread(self):
        self._running = True
        def refresh_loop():
            while self._running:
                time.sleep(60)
                if self._needs_refresh():
                    try:
                        self.log.info(f"{self.address}:{self.port} > refreshing session")
                        self.login()
                    except Exception:
                        pass
        thread = threading.Thread(target=refresh_loop, daemon=True)
        thread.start()

    def _needs_refresh(self):
        if not self.last_login:
            return True
        return (datetime.now() - self.last_login) > timedelta(minutes=self.refresh_interval)