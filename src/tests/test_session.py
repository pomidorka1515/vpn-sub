from __future__ import annotations

from collections.abc import Iterator
from typing import Unpack, cast, Literal
from config import JsonValue
from custom_types import RequestKwargs
from typing_contracts import SessionOptions, SessionExtras
from pathlib import Path

import pytest
from requests import ConnectionError, Response, Timeout

from helpers import json_http, make_inbound, make_panel_client
from loggers import Colors, color_status
from session import XUiSession, _client_stamp_path, inbound_stamp_path

class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, RequestKwargs]] = []
        self.request_status = 200
        self.request_payload: dict[str, JsonValue] = {"success": True, "msg": "", "obj": []}
        self.request_error: BaseException | None = None
        self.health_status = 200
        self.health_payload: dict[str, JsonValue] = {"success": True, "msg": "", "obj": []}
        self.health_error: BaseException | None = None

    def request(self, method: str, url: str, **kwargs: Unpack[RequestKwargs]) -> Response:
        self.calls.append((method, url, kwargs))
        if url.endswith("panel/api/inbounds/list"):
            if self.health_error is not None:
                raise self.health_error
            return json_http(self.health_payload, self.health_status)
        if self.request_error is not None:
            raise self.request_error
        return json_http(self.request_payload, self.request_status)


class SessionArguments(SessionExtras):
    name: str
    address: str
    port: int | str
    uri: str
    api_token: str


def session_kwargs(**overrides: Unpack[SessionOptions]) -> SessionArguments:
    values: SessionArguments = {
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
        headers = kwargs.get("headers")
        assert headers is not None
        assert headers["Authorization"] == f"Bearer {'x' * 40}"


def test_authorization_is_not_overridable_by_caller_headers(
    panel: XUiSession, transport: RecordingTransport,
) -> None:
    transport.calls.clear()
    panel.get(
        "panel/api/server/status",
        headers={"Authorization": "Bearer attacker-token"},
    )
    headers = transport.calls[0][2].get("headers")
    assert headers is not None
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
        headers = transport.calls[0][2].get("headers")
        assert headers is not None
        assert headers["X-Panel"] == "local"
        assert headers["Accept"] == "application/json"
        assert str(headers["Authorization"]).startswith("Bearer ")
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


def test_clear_cache_invalidates_another_session(tmp_path: Path, clock: FakeClock) -> None:
    stamp = tmp_path / "inbounds.stamp"
    first = XUiSession(**session_kwargs(transport=RecordingTransport(), clock=clock, stamp_path=str(stamp)))
    second = XUiSession(**session_kwargs(transport=RecordingTransport(), clock=clock, stamp_path=str(stamp)))
    try:
        first.cache = [make_inbound(1)]
        second.cache = [make_inbound(1)]
        assert first.cache_current is True
        assert second.cache_current is True
        first.clear_cache()
        assert first.cache is None
        assert second.cache is not None
        assert second.cache_current is False
    finally:
        first.close()
        second.close()


def test_clear_cache_keeps_the_list_when_the_stamp_cannot_move(panel: XUiSession) -> None:
    panel.cache = [make_inbound(1)]
    panel._stamp_path = "/proc/does-not-exist/inbounds.stamp"  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(OSError):
        panel.clear_cache()
    assert panel.cache is not None


def test_fresh_cache_misses_when_the_stamp_moves(tmp_path: Path, clock: FakeClock) -> None:
    stamp = tmp_path / "inbounds.stamp"
    first = XUiSession(**session_kwargs(transport=RecordingTransport(), clock=clock, stamp_path=str(stamp)))
    second = XUiSession(**session_kwargs(transport=RecordingTransport(), clock=clock, stamp_path=str(stamp)))
    try:
        first.cache = [make_inbound(1)]
        assert first.fresh_cache(15) is not None
        second.clear_cache()
        assert first.fresh_cache(15) is None
        assert first.cache is not None
    finally:
        first.close()
        second.close()


def test_cache_fill_does_not_revive_a_cleared_stamp(tmp_path: Path, clock: FakeClock) -> None:
    stamp = tmp_path / "inbounds.stamp"
    session = XUiSession(**session_kwargs(transport=RecordingTransport(), clock=clock, stamp_path=str(stamp)))
    try:
        session.cache = [make_inbound(1)]
        seen = session._cache_stamp  # pyright: ignore[reportPrivateUsage]
        session.clear_cache()
        session._cache_stamp = seen  # pyright: ignore[reportPrivateUsage]
        session.cache = [make_inbound(2)]
        assert session.cache is None
    finally:
        session.close()


def test_inbound_stamp_path_stays_inside_its_directory() -> None:
    path = inbound_stamp_path("../other", "http://127.0.0.1:1/a/", directory="/tmp/runtime")
    other = inbound_stamp_path("other", "http://127.0.0.1:2/a/", directory="/tmp/runtime")
    assert path.startswith("/tmp/runtime/inbounds.")
    assert ".." not in path
    assert path != other
    clients = _client_stamp_path("../other", "http://127.0.0.1:1/a/", directory="/tmp/runtime")
    assert clients.startswith("/tmp/runtime/clients.")
    assert clients != path


def test_client_cache_uses_its_own_stamp(tmp_path: Path, clock: FakeClock) -> None:
    inbound_stamp = tmp_path / "inbounds.stamp"
    client_stamp = tmp_path / "clients.stamp"
    session = XUiSession(**session_kwargs(
        transport=RecordingTransport(),
        clock=clock,
        stamp_path=str(inbound_stamp),
        client_stamp_path=str(client_stamp),
    ))
    try:
        session.cache = [make_inbound(1)]
        session.clients_cache = {"alice": make_panel_client("alice", [1])}
        assert session.fresh_cache(15) is not None
        assert session.fresh_clients(4) is not None
        session.clear_cache()
        assert session.fresh_cache(15) is None
        assert session.fresh_clients(4) is not None
        session.clear_clients()
        assert session.clients_cache is None
        assert session.fresh_clients(4) is None
    finally:
        session.close()


def test_client_stamp_must_not_be_the_inbound_stamp(tmp_path: Path) -> None:
    stamp = tmp_path / "shared.stamp"
    with pytest.raises(ValueError, match="client stamp"):
        XUiSession(**session_kwargs(
            transport=RecordingTransport(),
            stamp_path=str(stamp),
            client_stamp_path=str(stamp),
        ))


def test_rejects_transport_and_session_together(transport: RecordingTransport) -> None:
    from requests import Session

    with pytest.raises(ValueError, match="either transport or session"):
        XUiSession(**session_kwargs(transport=transport, session=Session()))


def test_invalid_mode_is_rejected(transport: RecordingTransport) -> None:
    with pytest.raises(ValueError, match="whitelist"):
        XUiSession(**session_kwargs(transport=transport, mode=cast(Literal["whitelist", "blacklist"], "both")))


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
    headers = kwargs.get("headers")
    assert headers is not None
    assert headers["Authorization"] == f"Bearer {'x' * 40}"


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


def test_verbose_logs_completed_requests(
    transport: RecordingTransport, clock: FakeClock, caplog: pytest.LogCaptureFixture,
) -> None:
    session = XUiSession(**session_kwargs(
        transport=transport, clock=clock, uri="secret/panel", verbose=True,
    ))
    session.log.addHandler(caplog.handler)
    try:
        with caplog.at_level("DEBUG", logger=session.log.name):
            response = session.get("panel/api/inbounds/list?token=secret")
            session._perform_health_check()  # pyright: ignore[reportPrivateUsage]
            session.dead = True
            down = session.get("panel/api/inbounds/list")
    finally:
        session.log.removeHandler(caplog.handler)
        session.close()

    size = len(response.content)
    expected = (
        f'"GET /panel/api/inbounds/list HTTP/1.1" '
        f"{color_status('200')} {size}b"
    )
    assert caplog.messages.count(expected) == 2
    assert color_status("200") == f"{Colors.GREEN}200{Colors.RESET}"
    assert "secret" not in "".join(caplog.messages)
    assert down.status_code == 503
    assert not any("503" in message for message in caplog.messages)


def test_verbose_defaults_to_quiet(
    panel: XUiSession, caplog: pytest.LogCaptureFixture,
) -> None:
    panel.log.addHandler(caplog.handler)
    try:
        with caplog.at_level("DEBUG", logger=panel.log.name):
            panel.get("panel/api/server/status")
    finally:
        panel.log.removeHandler(caplog.handler)

    assert panel.verbose is False
    assert not any("HTTP/1.1" in message for message in caplog.messages)
