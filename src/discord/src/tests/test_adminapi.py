from __future__ import annotations

from discord_helpers import json_ok, make_admin_client, run


def test_admin_client_sends_authorization_header() -> None:
    def handler(method: str, url: str, json: object, params: object, headers: object) -> object:
        del method, json, params
        assert url.endswith("/api/user/list")
        assert headers == {"Authorization": "api-token"}
        return json_ok(["alice"])

    client, session = make_admin_client(handler)
    result = run(client.list_users())
    assert result.ok
    assert result.obj == ["alice"]
    assert str(session.calls[0]["url"]).endswith("/sub/adminapi/api/user/list")


def test_admin_client_user_update_and_history() -> None:
    def handler(method: str, url: str, json: object, params: object, headers: object) -> object:
        del headers
        if url.endswith("/api/user/update"):
            assert method == "POST"
            assert json == {"user": "alice", "limit": 12}
            return json_ok(msg="Updated")
        if url.endswith("/api/user/history"):
            assert method == "GET"
            assert params == {"user": "alice", "days": 14}
            return json_ok([{"ts": 1, "up": 1, "down": 2, "wl_up": 0, "wl_down": 0}])
        raise AssertionError(url)

    client, _session = make_admin_client(handler)
    assert run(client.update_user("alice", limit=12)).ok
    history = run(client.history("alice", 14))
    assert history.ok
    assert history.obj[0]["up"] == 1
