from __future__ import annotations

from discord_helpers import FakeHeaders, FakeResponse, json_ok, make_web_client, run
from webapi import extract_auth_token


def test_extracts_secure_cookie_on_http() -> None:
    headers = FakeHeaders(
        {
            "Set-Cookie": "auth_token=secret-token; HttpOnly; Secure; SameSite=Lax; Max-Age=2592000",
        }
    )
    assert extract_auth_token(headers) == "secret-token"


def test_login_parses_secure_cookie() -> None:
    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del method, url, json, params, headers
        return FakeResponse(
            200,
            {
                "Set-Cookie": "auth_token=abc123; HttpOnly; Secure; SameSite=Lax",
            },
            {"success": True, "msg": "Successful login", "obj": {"username": "u"}},
        )

    client, _session = make_web_client(handler)
    result = run(client.login("u", "p"))
    assert result.ok
    assert result.auth_token == "abc123"


def test_register_then_login() -> None:
    calls: list[str] = []

    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del json, params, headers
        calls.append(f"{method} {url}")
        if url.endswith("/register"):
            return FakeResponse(201, {}, {"success": True, "msg": "Created", "obj": {"username": "web_x"}})
        if url.endswith("/login"):
            return FakeResponse(
                200,
                {"Set-Cookie": "auth_token=tok; Secure"},
                {"success": True, "msg": "Successful login", "obj": {"username": "u"}},
            )
        raise AssertionError(url)

    client, _session = make_web_client(handler)
    result = run(client.register("u", "p", "code", "Name"))
    assert result.ok
    assert result.auth_token == "tok"
    assert len(calls) == 2
    assert calls[0].endswith("/register")
    assert calls[1].endswith("/login")


def test_429_is_not_retried() -> None:
    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del method, url, json, params, headers
        return FakeResponse(429, {}, {"success": False, "msg": "429 Too many requests", "obj": None})

    client, session = make_web_client(handler)
    result = run(client.login("u", "p"))
    assert not result.ok
    assert result.status == 429
    assert len(session.calls) == 1


def test_stats_sends_cookie_and_caches() -> None:
    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del method, json, params, headers
        assert url.endswith("/stats")
        return json_ok({"displayname": "alice"})

    client, session = make_web_client(handler)
    first = run(client.stats("tok"))
    second = run(client.stats("tok"))
    assert first.ok
    assert first is second
    assert len(session.calls) == 1
    assert session.calls[0]["headers"] == {"Cookie": "auth_token=tok"}


def test_bonus_posts_code_and_invalidates_stats() -> None:
    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del params, headers
        if url.endswith("/stats"):
            return json_ok({"displayname": "alice"})
        assert method == "POST"
        assert url.endswith("/bonus")
        assert json == {"code": "BONUS1"}
        return json_ok()

    client, session = make_web_client(handler)
    run(client.stats("tok"))
    result = run(client.bonus("tok", "BONUS1"))
    assert result.ok
    run(client.stats("tok"))
    assert len(session.calls) == 3


def test_settings_omits_empty_fields() -> None:
    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del params, headers
        assert method == "POST"
        assert url.endswith("/settings")
        assert json == {"name": "Ada"}
        return json_ok()

    client, _session = make_web_client(handler)
    result = run(client.settings("tok", name="Ada", username=""))
    assert result.ok


def test_logout_reset_delete_history_qr() -> None:
    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del headers
        if url.endswith("/logout"):
            assert method == "POST"
            return json_ok()
        if url.endswith("/reset"):
            assert method == "POST"
            return json_ok()
        if url.endswith("/delete"):
            assert method == "POST"
            assert json == {"current_password": "secret"}
            return json_ok()
        if url.endswith("/history"):
            assert method == "GET"
            assert params == {"days": 14}
            return json_ok([{"ts": 1, "up": 1, "down": 2, "wl_up": 0, "wl_down": 0}])
        if url.endswith("/qr"):
            assert method == "GET"
            assert params == {"lang": "en"}
            return FakeResponse(200, {}, body=b"png-bytes", content_type="image/png")
        raise AssertionError(url)

    client, _session = make_web_client(handler)
    assert run(client.logout("tok")).ok
    assert run(client.reset("tok")).ok
    assert run(client.delete("tok", "secret")).ok
    history = run(client.history("tok", 14))
    assert history.ok
    qr = run(client.qr("tok", happ=False, lang="en"))
    assert qr.ok
    assert qr.body == b"png-bytes"

def test_validate_username_sends_params_and_passthrough() -> None:
    obj = {"valid": True, "taken": False, "sanitized": "alice"}

    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del json, headers
        assert method == "GET"
        assert url.endswith("/validate")
        assert params == {"username": "alice"}
        return json_ok(obj)

    client, session = make_web_client(handler)
    result = run(client.validate_username("alice"))
    assert result.ok
    assert result.obj == obj
    assert session.calls[0]["params"] == {"username": "alice"}
    assert str(session.calls[0]["url"]).endswith("/validate")
