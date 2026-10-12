from __future__ import annotations

import asyncio
import json
import socket
from typing import TYPE_CHECKING, Unpack

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from app_runtime.tasks import CapacityExceededError
from errors import XUiSessionError
from session import (
    AiohttpPanelTransport,
    AsyncXUiSession,
    FakeAsyncPanelTransport,
    PanelResponse,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from session.async_transport import AsyncRequestKwargs, AsyncXUiPanelTransport


def client(transport: AsyncXUiPanelTransport, *, verbose: bool = False) -> AsyncXUiSession:
    return AsyncXUiSession(
        name="test", address="127.0.0.1", port=2053, uri="/secret/panel/",
        api_token="private-token", transport=transport, health_check_interval=3600,
        inject_headers={"X-Panel": "injected", "authorization": "bad-token"},
        verbose=verbose,
    )


@pytest_asyncio.fixture
async def endpoint() -> AsyncIterator[str]:
    async def echo(request: web.Request) -> web.Response:
        return web.json_response({
            "auth": request.headers.get("Authorization"),
            "header": request.headers.get("X-Test"),
            "body": await request.json() if request.can_read_body else None,
        })

    app = web.Application()
    app.router.add_route("*", "/echo", echo)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    site = web.SockSite(runner, sock)
    await site.start()
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}/echo"
    finally:
        await runner.cleanup()


def test_buffered_response_is_independent_and_case_insensitive() -> None:
    response = PanelResponse.from_json({"success": True})
    assert response.json() == {"success": True}
    assert response.headers["content-type"] == "application/json"
    with pytest.raises(json.JSONDecodeError):
        PanelResponse(200, b"invalid").json()


@pytest.mark.asyncio
async def test_lifecycle_headers_url_and_normal_response() -> None:
    transport = FakeAsyncPanelTransport(PanelResponse.from_json({"success": True}))
    panel = client(transport)
    assert not transport.calls
    with pytest.raises(RuntimeError, match="not running"):
        await panel.get("panel/api/server/status")
    await panel.start()
    await panel.start()
    try:
        response = await panel.post(
            "panel/api/server/status", headers={"X-Panel": "caller", "AUTHORIZATION": "evil"},
            json={"value": 1},
        )
        assert response.json() == {"success": True}
        method, url, kwargs = transport.calls[0]
        assert method == "POST"
        assert url == "http://127.0.0.1:2053/secret/panel/api/server/status"
        headers = kwargs.get("headers")
        assert headers is not None
        assert headers["Authorization"] == "Bearer private-token"
        assert headers["X-Panel"] == "caller"
        assert len([key for key in headers if key.lower() == "authorization"]) == 1
        with pytest.raises(ValueError, match="base URL"):
            await panel.get("https://other.example/secret")
    finally:
        await panel.close()
    await panel.close()
    with pytest.raises(RuntimeError, match="closed"):
        await panel.start()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_token_rejection_is_permanent(status: int) -> None:
    transport = FakeAsyncPanelTransport(PanelResponse.from_json({}, status))
    panel = client(transport)
    await panel.start()
    try:
        assert (await panel.get("panel/api/inbounds/list")).status_code == status
        assert panel.dead
        await panel.check_health()
        assert (await panel.get("panel/api/server/status")).status_code == 503
        assert len(transport.calls) == 1
    finally:
        await panel.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    TimeoutError("secret-token"), aiohttp.ClientConnectionError("secret-url"),
    aiohttp.ClientError("secret-query"),
])
async def test_unavailable_recovery_and_redaction(
    error: Exception, capfd: pytest.CaptureFixture[str],
) -> None:
    transport = FakeAsyncPanelTransport(
        error, PanelResponse.from_json({"success": True}),
        PanelResponse.from_json({"success": True}),
    )
    panel = client(transport, verbose=True)
    await panel.start()
    try:
        response = await panel.get("panel/api/server/status?token=private-query")
        assert response.status_code == 503
        assert bool(panel.dead)
        await panel.check_health()
        assert not panel.dead
        await panel.get("panel/api/server/status?token=private-query")
        output = capfd.readouterr().err
        assert all(value not in output for value in (
            "secret-token", "secret-url", "secret-query", "private-token", "private-query", "/secret/",
        ))
        assert "panel/api/server/status" in output
    finally:
        await panel.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    PanelResponse(200, b"not json"), PanelResponse.from_json({"success": False, "msg": "secret"}),
    PanelResponse.from_json([], 200), PanelResponse.from_json({}, 500),
])
async def test_invalid_health_response_marks_dead(response: PanelResponse) -> None:
    panel = client(FakeAsyncPanelTransport(response))
    await panel.start()
    try:
        await panel.check_health()
        assert panel.dead
    finally:
        await panel.close()


class BlockingTransport:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def request(
        self, method: str, url: str, **kwargs: Unpack[AsyncRequestKwargs],
    ) -> PanelResponse:
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return PanelResponse.from_json({"success": True})


@pytest.mark.asyncio
async def test_cancellation_propagates_without_marking_dead() -> None:
    transport = BlockingTransport()
    panel = client(transport)
    await panel.start()
    try:
        task = asyncio.create_task(panel.get("panel/api/inbounds/list"))
        await transport.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert transport.cancelled.is_set()
        assert not panel.dead
    finally:
        await panel.close()


@pytest.mark.asyncio
async def test_close_drains_inflight_and_survives_waiter_cancellation() -> None:
    transport = BlockingTransport()
    panel = client(transport)
    await panel.start()
    request = asyncio.create_task(panel.get("panel/api/inbounds/list"))
    await transport.entered.wait()
    closing = asyncio.create_task(panel.close())
    await asyncio.sleep(0)
    assert not closing.done()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    transport.release.set()
    await request
    await panel.close()


@pytest.mark.asyncio
async def test_bounded_client_admission() -> None:
    transport = BlockingTransport()
    panel = AsyncXUiSession(
        name="test", address="127.0.0.1", port=2053, uri="secret",
        api_token="token", transport=transport, request_limit=1,
    )
    await panel.start()
    request = asyncio.create_task(panel.get("panel/api/inbounds/list"))
    await transport.entered.wait()
    try:
        with pytest.raises(CapacityExceededError):
            await panel.get("panel/api/server/status")
    finally:
        transport.release.set()
        await request
        await panel.close()


@pytest.mark.asyncio
async def test_real_aiohttp_buffering_auth_and_injected_session_ownership(endpoint: str) -> None:
    async with aiohttp.ClientSession() as session:
        transport = AiohttpPanelTransport(session)
        await transport.start()
        response = await transport.request(
            "POST", endpoint, headers={"Authorization": "Bearer token", "X-Test": "header"},
            json={"value": 1},
        )
        assert response.json() == {
            "auth": "Bearer token", "header": "header", "body": {"value": 1},
        }
        assert response.headers["content-type"].startswith("application/json")
        await transport.close()
        assert not session.closed
        # The buffered body remains usable after the response/session close.
    assert response.json() is not None


@pytest.mark.asyncio
async def test_real_basic_auth_and_bearer_conflict(endpoint: str) -> None:
    transport = AiohttpPanelTransport(auth=("user", "password"))
    await transport.start()
    try:
        response = await transport.request("GET", endpoint)
        payload = response.json()
        assert isinstance(payload, dict)
        assert str(payload["auth"]).startswith("Basic ")
        with pytest.raises(XUiSessionError, match="basic auth alongside"):
            await transport.request("GET", endpoint, headers={"authorization": "Bearer token"})
    finally:
        await transport.close()
    assert transport._session is not None
    assert transport._session.closed
    await transport.close()


@pytest.mark.asyncio
async def test_injected_session_default_auth_conflict(endpoint: str) -> None:
    async with aiohttp.ClientSession(auth=aiohttp.BasicAuth("user", "password")) as session:
        transport = AiohttpPanelTransport(session)
        await transport.start()
        try:
            with pytest.raises(XUiSessionError, match="basic auth alongside"):
                await transport.request("GET", endpoint, headers={"Authorization": "Bearer token"})
        finally:
            await transport.close()


@pytest.mark.asyncio
async def test_close_before_start() -> None:
    transport = AiohttpPanelTransport()
    assert transport._session is None
    await transport.close()
    with pytest.raises(RuntimeError, match="closed"):
        await transport.start()


@pytest.mark.asyncio
async def test_real_timeout_cancellation_and_transport_limit() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow(request: web.Request) -> web.Response:
        entered.set()
        await release.wait()
        return web.json_response({"success": True})

    app = web.Application()
    app.router.add_get("/slow", slow)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    await web.SockSite(runner, sock).start()
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/slow"
    transport = AiohttpPanelTransport(limit=1)
    await transport.start()
    try:
        with pytest.raises(TimeoutError):
            await transport.request("GET", url, timeout=0.02)
        task = asyncio.create_task(transport.request("GET", url))
        # The owned transport task must enter admission before saturation.
        await asyncio.sleep(0)
        with pytest.raises(CapacityExceededError):
            await transport.request("GET", url)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert (await transport.request("GET", url)).status_code == 200
    finally:
        release.set()
        await transport.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_health_task_is_started_explicitly_and_settled_on_close() -> None:
    transport = BlockingTransport()
    panel = AsyncXUiSession(
        name="test", address="127.0.0.1", port=2053, uri="secret",
        api_token="token", transport=transport, health_check_interval=1,
    )
    assert panel._health.active_count == 0
    await panel.start()
    assert panel._health.active_count == 1
    try:
        await asyncio.wait_for(transport.entered.wait(), timeout=3)
    finally:
        await panel.close()
    assert transport.cancelled.is_set()
    assert panel._health.active_count == 0
    assert panel._requests.active_count == 0


@pytest.mark.asyncio
async def test_client_does_not_close_injected_session(endpoint: str) -> None:
    async with aiohttp.ClientSession() as session:
        panel = AsyncXUiSession(
            name="test", address="127.0.0.1", port=2053, uri="secret",
            api_token="token", session=session,
        )
        await panel.start()
        await panel.close()
        assert not session.closed
