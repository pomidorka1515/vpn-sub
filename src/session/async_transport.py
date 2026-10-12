"""Buffered, application-owned responses and explicitly started async HTTP."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, TypedDict, Unpack, cast

import aiohttp
from multidict import CIMultiDict, CIMultiDictProxy

from app_runtime.tasks import TaskRegistry
from errors import XUiSessionError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from config import JsonValue


class AsyncRequestKwargs(TypedDict, total=False):
    headers: Mapping[str, str]
    params: Mapping[str, str | int | float]
    json: JsonValue
    data: Mapping[str, str] | bytes | str
    timeout: float
    auth: tuple[str, str] | None


@dataclass(frozen=True, slots=True)
class PanelResponse:
    status_code: int
    content: bytes
    headers: Mapping[str, str] = field(default_factory=dict[str, str])

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", CIMultiDictProxy(CIMultiDict(self.headers)))

    def json(self) -> JsonValue:
        """Decode independently of transport, including malformed-body errors."""
        return cast("JsonValue", json.loads(self.content))

    @classmethod
    def from_json(cls, payload: JsonValue, status_code: int = 200) -> PanelResponse:
        return cls(status_code, json.dumps(payload).encode(), {"Content-Type": "application/json"})


class AsyncXUiPanelTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        **kwargs: Unpack[AsyncRequestKwargs],
    ) -> PanelResponse: ...


class AiohttpPanelTransport:
    """Own a session unless injected; refuse conflicting HTTP auth explicitly.

    Admission rejects saturation rather than queueing unbounded HTTP tasks.
    All admitted requests settle before closing the owned session.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession | None = None,
        auth: tuple[str, str] | None = None,
        *,
        limit: int = 8,
        timeout: float = 5.0,
        connect_timeout: float = 5.0,
        read_timeout: float = 5.0,
    ) -> None:
        if min(timeout, connect_timeout, read_timeout) <= 0:
            raise ValueError("timeouts must be positive")
        self._session = session
        self._owns_session = session is None
        self._auth = auth
        self._limit = limit
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        # Awaited request errors are handled by the client. Raw HTTP exception
        # tracebacks may contain credentials and must not enter task logs.
        self._requests = TaskRegistry(capacity=limit, report_failures=False)
        self._started = False
        self._close_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._close_task is not None:
            raise RuntimeError("transport is closed")
        if self._started:
            return
        if self._session is None:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=self._limit, limit_per_host=self._limit),
                trust_env=False,
            )
        if self._session.closed:
            raise RuntimeError("injected session is closed")
        self._started = True

    async def request(
        self,
        method: str,
        url: str,
        **kwargs: Unpack[AsyncRequestKwargs],
    ) -> PanelResponse:
        if not self._started:
            raise RuntimeError("transport has not started")
        task = self._requests.create(
            lambda: self._request(method, url, **kwargs),
            name="panel-http",
        )
        return await task

    async def _request(
        self,
        method: str,
        url: str,
        **kwargs: Unpack[AsyncRequestKwargs],
    ) -> PanelResponse:
        session = self._session
        if session is None:
            raise RuntimeError("transport has not started")
        auth = kwargs.get("auth", self._auth)
        headers = kwargs.get("headers", {})
        has_authorization = any(key.lower() == "authorization" for key in headers)
        session_has_authorization = "Authorization" in session.headers
        # aiohttp also rejects this combination; check it here for a stable,
        # credential-free application error instead of a library exception.
        if (auth is not None or session.auth is not None) and (
            has_authorization or session_has_authorization
        ):
            raise XUiSessionError(
                "refusing to send basic auth alongside an Authorization header",
            )
        total = kwargs.get("timeout", self._timeout)
        if total <= 0:
            raise ValueError("timeout must be positive")
        deadline = aiohttp.ClientTimeout(
            total=total,
            connect=min(total, self._connect_timeout),
            sock_read=min(total, self._read_timeout),
        )
        async with session.request(
            method,
            url,
            headers=headers,
            params=kwargs.get("params"),
            json=kwargs.get("json"),
            data=kwargs.get("data"),
            timeout=deadline,
            auth=aiohttp.BasicAuth(*auth) if auth is not None else None,
            allow_redirects=False,
        ) as response:
            return PanelResponse(response.status, await response.read(), response.headers)

    async def close(self) -> None:
        if self._close_task is None:
            self._requests.stop_admission()
            self._close_task = asyncio.create_task(self._close(), name="panel-transport-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        await self._requests.drain()
        if self._owns_session and self._session is not None:
            await self._session.close()


class FakeAsyncPanelTransport:
    """Script buffered responses/errors without real network or lifecycle work."""

    def __init__(self, *outcomes: PanelResponse | Exception) -> None:
        self.outcomes = deque(outcomes)
        self.calls: list[tuple[str, str, AsyncRequestKwargs]] = []

    async def request(
        self,
        method: str,
        url: str,
        **kwargs: Unpack[AsyncRequestKwargs],
    ) -> PanelResponse:
        self.calls.append((method, url, kwargs))
        if not self.outcomes:
            raise RuntimeError("fake transport has no remaining outcomes")
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
