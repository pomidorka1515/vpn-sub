from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest
from requests import ConnectionError, Response, Timeout

from errors import XUiSessionError
from helpers import json_http, make_inbound
from session import XUiSession
from custom_types import Inbound

class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.login_status = 200
        self.login_payload: dict[str, Any] = {"success": True, "msg": ""}
        self.login_error: BaseException | None = None
        self.request_status = 200
        self.request_payload: dict[str, Any] = {"success": True, "msg": "", "obj": []}
        self.request_error: BaseException | None = None
        self.health_status = 200
        self.health_payload: dict[str, Any] = {"success": True, "msg": "", "obj": []}
        self.health_error: BaseException | None = None

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        self.calls.append((method, url, kwargs))
        if url.endswith("/login"):
            if self.login_error is not None:
                raise self.login_error
            return json_http(self.login_payload, self.login_status)
        if url.endswith("panel/api/inbounds/list"):
            if self.health_error is not None:
                raise self.health_error
            return json_http(self.health_payload, self.health_status)
        if self.request_error is not None:
            raise self.request_error
        return json_http(self.request_payload, self.request_status)


def session_kwargs(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": "local",
        "address": "127.0.0.1",
        "port": 2053,
        "uri": "secret",
        "username": "admin",
        "password": "pass",
        "health_check_interval": 3600,
        "refresh_interval": 60,
    }
    values.update(overrides)
    return values


@pytest.fixture
def transport() -> RecordingTransport:
    return RecordingTransport()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def panel(transport: RecordingTransport, clock: FakeClock) -> Iterator[XUiSession]:
    session = XUiSession(**session_kwargs(transport=transport, clock=clock))
    try:
        yield session
    finally:
        session.close()


def test_login_posts_credentials_and_starts_refresh(
    panel: XUiSession, transport: RecordingTransport,
) -> None:
    assert transport.calls[0][0] == "POST"
    assert transport.calls[0][1] == "http://127.0.0.1:2053/secret/login"
    assert transport.calls[0][2]["json"] == {"username": "admin", "password": "pass"}
    assert panel._refresh_started  # pyright: ignore[reportPrivateUsage]
    assert panel.local is True
    assert panel.base_url == "http://127.0.0.1:2053/secret/"


def test_failed_login_sets_auth_backoff(transport: RecordingTransport, clock: FakeClock) -> None:
    transport.login_status = 401
    with pytest.raises(XUiSessionError, match="authentication failed"):
        XUiSession(**session_kwargs(transport=transport, clock=clock))


def test_request_returns_down_response_when_dead(panel: XUiSession) -> None:
    panel.dead = True
    response = panel.get("panel/api/inbounds/list")
    assert response.status_code == 503
    payload = response.json()
    assert payload["success"] is False
    assert "unavailable" in payload["msg"]


def test_request_timeout_marks_panel_dead(
    panel: XUiSession, transport: RecordingTransport,
) -> None:
    transport.request_error = Timeout()
    response = panel.post("panel/api/inbounds/addClient")
    assert response.status_code == 503
    assert panel.dead is True
    assert "timeout" in response.json()["msg"]


def test_connection_error_marks_panel_dead(
    panel: XUiSession, transport: RecordingTransport,
) -> None:
    transport.request_error = ConnectionError("refused")
    response = panel.get("panel/api/server/status")
    assert response.status_code == 503
    assert panel.dead is True
    assert "connection error" in response.json()["msg"]


def test_refresh_relogs_when_interval_elapsed(
    panel: XUiSession, transport: RecordingTransport, clock: FakeClock,
) -> None:
    transport.calls.clear()
    clock.advance(61 * 60)
    response = panel.get("panel/api/server/status")
    assert response.status_code == 200
    assert transport.calls[0][1].endswith("/login")
    assert transport.calls[1][1].endswith("panel/api/server/status")


def test_refresh_login_failure_returns_down_response(
    panel: XUiSession, transport: RecordingTransport, clock: FakeClock,
) -> None:
    transport.calls.clear()
    clock.advance(61 * 60)
    transport.login_status = 500
    response = panel.get("panel/api/server/status")
    assert response.status_code == 503
    assert response.json()["success"] is False


def test_inject_headers_are_merged_into_requests(
    transport: RecordingTransport, clock: FakeClock,
) -> None:
    session = XUiSession(
        **session_kwargs(
            transport=transport,
            clock=clock,
            inject_headers={"X-Panel": "local"},
        ),
    )
    try:
        transport.calls.clear()
        session.get("panel/api/server/status", headers={"Accept": "application/json"})
        headers = transport.calls[0][2]["headers"]
        assert headers["X-Panel"] == "local"
        assert headers["Accept"] == "application/json"
    finally:
        session.close()


def test_cache_roundtrip_and_clear(panel: XUiSession, clock: FakeClock) -> None:
    inbound = make_inbound(1)
    panel.cache = [inbound]
    assert panel.cache is not None
    assert cast(list[Inbound], panel.cache)[0].id == 1
    assert panel.cache_time == clock.now
    panel.clear_cache()
    assert panel.cache is None
    assert panel.cache_time == 0 # type: ignore[unreachable]


def test_rejects_transport_and_session_together(transport: RecordingTransport) -> None:
    from requests import Session

    with pytest.raises(ValueError, match="either transport or session"):
        XUiSession(**session_kwargs(transport=transport, session=Session()))


def test_invalid_mode_is_rejected(transport: RecordingTransport) -> None:
    with pytest.raises(ValueError, match="whitelist"):
        XUiSession(**session_kwargs(transport=transport, mode="both"))


def test_health_check_marks_dead_on_unsuccessful_payload(
    panel: XUiSession, transport: RecordingTransport,
) -> None:
    transport.health_payload = {"success": False, "msg": "nope"}
    panel._perform_health_check()  # pyright: ignore[reportPrivateUsage]
    assert panel.dead is True


def test_health_check_recovers_after_successful_payload(
    panel: XUiSession, transport: RecordingTransport,
) -> None:
    panel.dead = True
    panel._perform_health_check()  # pyright: ignore[reportPrivateUsage]
    assert panel.dead is False


def test_health_check_does_not_login_during_auth_backoff(
    transport: RecordingTransport, clock: FakeClock,
) -> None:
    transport.login_status = 401
    with pytest.raises(XUiSessionError):
        XUiSession(**session_kwargs(transport=transport, clock=clock))

    transport.login_status = 200
    session = XUiSession(**session_kwargs(transport=transport, clock=clock))
    try:
        session._login_retry_at = clock.now + 30  # pyright: ignore[reportPrivateUsage]
        transport.health_status = 401
        login_calls_before = sum(
            1 for _method, url, _kwargs in transport.calls if url.endswith("/login")
        )
        session._perform_health_check()  # pyright: ignore[reportPrivateUsage]
        login_calls_after = sum(
            1 for _method, url, _kwargs in transport.calls if url.endswith("/login")
        )
        assert login_calls_after == login_calls_before
        assert session.dead is True
    finally:
        session.close()


def test_close_stops_background_threads(panel: XUiSession) -> None:
    panel.close()
    assert panel._stop_event.is_set()  # pyright: ignore[reportPrivateUsage]
    assert panel._health_check_event.is_set()  # pyright: ignore[reportPrivateUsage]
    assert not panel._health_check_thread.is_alive()  # pyright: ignore[reportPrivateUsage]
    if panel._refresh_thread is not None:  # pyright: ignore[reportPrivateUsage]
        assert not panel._refresh_thread.is_alive()  # pyright: ignore[reportPrivateUsage]


def test_url_strips_panel_suffix_before_joining(
    transport: RecordingTransport, clock: FakeClock,
) -> None:
    session = XUiSession(
        **session_kwargs(transport=transport, clock=clock, uri="secret/panel"),
    )
    try:
        assert session._format_url("login") == "http://127.0.0.1:2053/secret/login"  # pyright: ignore[reportPrivateUsage]
    finally:
        session.close()
