"""Async panel client staged independently of synchronous core/cache callers."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Unpack
from urllib.parse import urlsplit

import aiohttp
from multidict import CIMultiDict

from app_runtime.tasks import TaskRegistry
from loggers import Logger, color_status

from .async_transport import AiohttpPanelTransport, PanelResponse

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .async_transport import AsyncRequestKwargs, AsyncXUiPanelTransport


class AsyncXUiSession:
    """Single-loop client with explicit startup and owned health/request tasks.

    Injected transports are already started and remain injector-owned. The
    default transport owns its session unless a session was injected. Rejected
    tokens are permanent for this client; transient failures can recover via
    health checks. Cancellation propagates without marking a panel unavailable.
    """

    def __init__(
        self, *, name: str, address: str, port: int | str, uri: str,
        api_token: str, https: bool = False,
        nginx_auth: tuple[str, str] | None = None,
        inject_headers: Mapping[str, str] | None = None,
        health_check_interval: float = 20.0, verbose: bool = False,
        transport: AsyncXUiPanelTransport | None = None,
        session: aiohttp.ClientSession | None = None,
        request_limit: int = 8, request_timeout: float = 5.0,
    ) -> None:
        if not api_token:
            raise ValueError("api_token must be a non-empty API token")
        if health_check_interval < 1:
            raise ValueError("health_check_interval must be at least 1")
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        if transport is not None and session is not None:
            raise ValueError("pass either transport or session, not both")
        self.name = name
        self.address = address
        self.port = str(port)
        self.local = address in {"localhost", "::1", "127.0.0.1", "0.0.0.0"}  # ruff: ignore[hardcoded-bind-all-interfaces]
        host = f"[{address}]" if ":" in address and not address.startswith("[") else address
        prefix = uri.strip("/").removesuffix("/panel")
        if prefix == "panel":
            prefix = ""
        self.base_url = f"{'https' if https else 'http'}://{host}:{port}/"
        if prefix:
            self.base_url += f"{prefix}/"
        self._api_token = api_token
        self._headers = dict(inject_headers or {})
        self._interval = health_check_interval
        self._timeout = request_timeout
        self.verbose = verbose
        self.log = Logger(type(self).__name__)
        self.dead = False
        self._token_rejected = False
        self._owned_transport = (
            AiohttpPanelTransport(session, nginx_auth, limit=request_limit, timeout=request_timeout)
            if transport is None else None
        )
        self._transport: AsyncXUiPanelTransport
        if self._owned_transport is not None:
            self._transport = self._owned_transport
        elif transport is not None:
            self._transport = transport
        else:
            raise RuntimeError("panel transport is missing")
        self._requests = TaskRegistry(capacity=request_limit, report_failures=False)
        self._health = TaskRegistry(capacity=1)
        self._started = False
        self._close_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._close_task is not None:
            raise RuntimeError("panel client is closed")
        if self._started:
            return
        if self._owned_transport is not None:
            await self._owned_transport.start()
        self._health.create(self._health_loop, name="panel-health")
        self._started = True

    def _format_url(self, url: str) -> str:
        if urlsplit(url).scheme or url.startswith("//"):
            if not url.startswith(self.base_url):
                raise ValueError("panel request URL must use the configured base URL")
            return url
        return self.base_url + url.lstrip("/")

    def _request_target(self, url: str) -> str:
        route = url.removeprefix(self.base_url).split("?", 1)[0]
        # Never log arbitrary absolute URLs, credentials or the secret prefix.
        return f"/{route}" if route.startswith("panel/api/") else "/<redacted>"

    def _down_response(self, reason: str) -> PanelResponse:
        return PanelResponse.from_json(
            {"success": False, "msg": f"Panel {self.name} is down: {reason}", "obj": None}, 503,
        )

    def _mark_dead(self, reason: str) -> None:
        was_alive = not self.dead
        self.dead = True
        if was_alive:
            self.log.warning(f"panel {self.name}: marked down: {reason}")

    async def request(
        self, method: str, url: str, **kwargs: Unpack[AsyncRequestKwargs],
    ) -> PanelResponse:
        if not self._started or self._close_task is not None:
            raise RuntimeError("panel client is not running")
        task = self._requests.create(
            lambda: self._request(method, url, health=False, **kwargs), name="panel-request",
        )
        return await task

    async def _request(
        self, method: str, url: str, *, health: bool,
        **kwargs: Unpack[AsyncRequestKwargs],
    ) -> PanelResponse:
        if self._token_rejected or (self.dead and not health):
            return self._down_response("unavailable")
        target = self._format_url(url)
        headers = CIMultiDict(self._headers)
        headers.update(kwargs.get("headers", {}))
        headers["Authorization"] = f"Bearer {self._api_token}"
        kwargs["headers"] = headers
        kwargs.setdefault("timeout", self._timeout)
        try:
            response = await self._transport.request(method, target, **kwargs)
        except TimeoutError:
            reason = "request timeout"
        except aiohttp.ClientConnectionError:
            reason = "connection error"
        except aiohttp.ClientError:
            reason = "request error"
        else:
            if self.verbose:
                self.log.debug(
                    f'"{method} {self._request_target(target)} HTTP/1.1" '
                    f"{color_status(str(response.status_code))} {len(response.content)}b",
                )
            if response.status_code in {401, 403}:
                self._token_rejected = True
                self._mark_dead(f"token rejected (HTTP {response.status_code})")
            return response
        self._mark_dead(reason)
        return self._down_response(reason)

    async def get(self, url: str, **kwargs: Unpack[AsyncRequestKwargs]) -> PanelResponse:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Unpack[AsyncRequestKwargs]) -> PanelResponse:
        return await self.request("POST", url, **kwargs)

    async def check_health(self) -> None:
        if not self._started or self._close_task is not None:
            raise RuntimeError("panel client is not running")
        task = self._requests.create(
            lambda: self._request("GET", "panel/api/inbounds/list", health=True),
            name="panel-health-request",
        )
        response = await task
        if self._token_rejected:
            return
        if response.status_code != 200:
            self._mark_dead(f"HTTP {response.status_code}")
            return
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._mark_dead("invalid health response")
            return
        if not isinstance(payload, dict) or not payload.get("success"):
            self._mark_dead("unsuccessful health response")
            return
        was_dead = self.dead
        self.dead = False
        if was_dead:
            self.log.info(f"panel {self.name}: recovered")

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            if self._token_rejected:
                return
            try:
                await self.check_health()
            except Exception:
                # Exceptions can contain URLs/credentials; don't stringify them.
                self.log.warning(f"panel {self.name}: health check failed")

    async def close(self) -> None:
        if self._close_task is None:
            self._requests.stop_admission()
            self._close_task = asyncio.create_task(self._close(), name="panel-client-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        await self._health.drain(cancel=True)
        await self._requests.drain()
        if self._owned_transport is not None:
            await self._owned_transport.close()
