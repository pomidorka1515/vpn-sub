from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from requests import ConnectionError, Response, Timeout

from helpers import json_http, make_inbound
from session import XUiSession

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
        self.request_status = 200
        self.request_payload: dict[str, Any] = {"success": True, "msg": "", "obj": []}
        self.request_error: BaseException | None = None
        self.health_status = 200
        self.health_payload: dict[str, Any] = {"success": True, "msg": "", "obj": []}
        self.health_error: BaseException | None = None

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        self.calls.append((method, url, kwargs))
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
        "api_token": "x" * 40,
        "health_check_interval": 3600,
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


def test_no_login_is_ever_attempted(panel: XUiSession, transport: RecordingTransport) -> None:
    assert not any(url.endswith("/login") for _method, url, _kwargs in transport.calls)
    assert panel.local is True
    assert panel.base_url == "http://127.0.0.1:2053/secret/"


def test_bearer_header_is_present_on_every_request(
    panel: XUiSession, transport: RecordingTransport,
) -> None:
    transport.calls.clear()
    panel.get("panel/api/server/status")
    panel.post("panel/api/clients/add", json={"client": {}})
    for _method, _url, kwargs in transport.calls:
        assert kwargs["headers"]["Authorization"] == f"Bearer {'x' * 40}"


def test_authorization_is_not_overridable_by_caller_headers(
    panel: XUiSession, transport: RecordingTransport,
) -> None:
    transport.calls.clear()
    panel.get(
        "panel/api/server/status",
        headers={"Authorization": "Bearer attacker-token"},
    )
    headers = transport.calls[0][2]["headers"]
    assert headers["Authorization"] == f"Bearer {'x' * 40}"


def test_empty_api_token_is_rejected(transport: RecordingTransport) -> None:
    with pytest.raises(ValueError, match="api_token"):
        XUiSession(**session_kwargs(transport=transport, api_token=""))


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
    response = panel.post("panel/api/clients/add")
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
        assert headers["Authorization"].startswith("Bearer ")
    finally:
        session.close()


def test_cache_roundtrip_and_clear(panel: XUiSession, clock: FakeClock) -> None:
    inbound = make_inbound(1)
    panel.cache = [inbound]
    cached = panel.cache
    assert cached is not None
    assert cached[0].id == 1
    assert panel.cache_time == clock.now
    panel.clear_cache()
    assert panel.cache is None
    assert panel.cache_time == 0


def test_cache_age_uses_the_session_clock(panel: XUiSession, clock: FakeClock) -> None:
    panel.cache = [make_inbound(1)]
    assert panel.cache_age == 0.0
    clock.advance(5)
    assert panel.cache_age == 5.0


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


def test_health_check_recovers_after_successful_payload(panel: XUiSession) -> None:
    panel.dead = True
    panel._perform_health_check()  # pyright: ignore[reportPrivateUsage]
    assert panel.dead is False


def test_health_check_marks_dead_with_token_rejected_on_401(
    panel: XUiSession, transport: RecordingTransport,
) -> None:
    transport.health_status = 401
    transport.calls.clear()
    panel._perform_health_check()  # pyright: ignore[reportPrivateUsage]
    assert panel.dead is True
    # no retry, no login: exactly the single health request
    assert len(transport.calls) == 1


def test_health_check_marks_dead_with_token_rejected_on_403(
    panel: XUiSession, transport: RecordingTransport,
) -> None:
    transport.health_status = 403
    panel._perform_health_check()  # pyright: ignore[reportPrivateUsage]
    assert panel.dead is True


def test_health_check_carries_bearer_header(
    panel: XUiSession, transport: RecordingTransport,
) -> None:
    transport.calls.clear()
    panel._perform_health_check()  # pyright: ignore[reportPrivateUsage]
    kwargs = transport.calls[0][2]
    assert kwargs["headers"]["Authorization"] == f"Bearer {'x' * 40}"


def test_close_stops_background_threads(panel: XUiSession) -> None:
    panel.close()
    assert panel._health_check_event.is_set()  # pyright: ignore[reportPrivateUsage]
    assert not panel._health_check_thread.is_alive()  # pyright: ignore[reportPrivateUsage]


def test_url_strips_panel_suffix_before_joining(
    transport: RecordingTransport, clock: FakeClock,
) -> None:
    session = XUiSession(
        **session_kwargs(transport=transport, clock=clock, uri="secret/panel"),
    )
    try:
        assert session._format_url("panel/api/server/status") == "http://127.0.0.1:2053/secret/panel/api/server/status"  # pyright: ignore[reportPrivateUsage]
    finally:
        session.close()
