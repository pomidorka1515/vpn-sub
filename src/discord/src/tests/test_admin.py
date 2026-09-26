from __future__ import annotations

from collections.abc import Mapping
from unittest.mock import patch

from discord_helpers import FakeInteraction, FakeResponse, json_ok, make_admin_bot, modal_data, run, stats_obj


def _ok(*args: object) -> FakeResponse:
    del args
    return json_ok()


def test_non_whitelist_admin_does_not_call_api() -> None:
    calls: list[str] = []

    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del method, json, params, headers
        calls.append(url)
        return json_ok()

    bot, session = make_admin_bot(handler, whitelist=[99])
    interaction = FakeInteraction(user_id=7)
    run(bot.cmd_admin(interaction))  # type: ignore[arg-type]
    assert "Нет доступа" in str(interaction.response.messages[-1]["content"])
    assert session.calls == []
    assert calls == []

    denied = FakeInteraction(user_id=7, data={"custom_id": "admin:list_users"})
    run(bot.dispatch_component(denied))  # type: ignore[arg-type]
    assert session.calls == []
    assert "Нет доступа" in str(denied.response.messages[-1]["content"])


def test_whitelist_admin_renders_main_menu() -> None:
    bot, session = make_admin_bot(_ok)
    interaction = FakeInteraction(user_id=7)
    run(bot.cmd_admin(interaction))  # type: ignore[arg-type]
    assert session.calls == []
    last = interaction.response.messages[-1]
    assert "Панель управления" in str(last["content"])
    view = last["view"]
    ids = [getattr(item, "custom_id", None) for item in getattr(view, "children", [])]
    assert "admin:list_users" in ids
    assert "admin:codes" in ids
    assert "admin:status_panels" in ids


def test_list_users_button_uses_admin_api() -> None:
    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del method, json, params
        assert headers == {"Authorization": "api-token"}
        assert url.endswith("/api/user/list")
        return json_ok(["alice", "bob"])

    bot, session = make_admin_bot(handler)
    interaction = FakeInteraction(user_id=7, data={"custom_id": "admin:list_users"})
    run(bot.dispatch_component(interaction))  # type: ignore[arg-type]
    assert len(session.calls) == 1
    content = str(interaction.response.messages[-1]["content"])
    assert "alice" in content
    assert "bob" in content


def test_codes_menu_does_not_call_api() -> None:
    bot, session = make_admin_bot(_ok)
    interaction = FakeInteraction(user_id=7, data={"custom_id": "admin:codes"})
    run(bot.dispatch_component(interaction))  # type: ignore[arg-type]
    assert session.calls == []
    last = interaction.response.messages[-1]
    ids = [getattr(item, "custom_id", None) for item in getattr(last["view"], "children", [])]
    assert "admin:list_codes" in ids


def test_panel_status_uses_admin_api() -> None:
    payload = {
        "n1": {
            "success": True,
            "msg": "",
            "obj": {
                "cpu": 10,
                "cpuCores": 2,
                "logicalPro": 4,
                "cpuSpeedMhz": 2400,
                "mem": {"current": 1, "total": 2},
                "swap": {"current": 0, "total": 1},
                "disk": {"current": 1, "total": 2},
                "xray": {"state": "running", "version": "1.8", "errorMsg": ""},
                "uptime": 60,
                "loads": [0.1, 0.2, 0.3],
                "tcpCount": 1,
                "udpCount": 2,
                "netIO": {"down": 0, "up": 0},
                "netTraffic": {"recv": 0, "sent": 0},
                "publicIP": {"ipv4": "1.1.1.1", "ipv6": ""},
                "appStats": {"threads": 1, "mem": 1, "uptime": 10},
            },
        }
    }

    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del method, json, params, headers
        assert url.endswith("/api/panel/status")
        return json_ok(payload)

    bot, session = make_admin_bot(handler)
    interaction = FakeInteraction(user_id=7, data={"custom_id": "admin:status_panels"})
    run(bot.dispatch_component(interaction))  # type: ignore[arg-type]
    assert len(session.calls) == 1
    texts = [str(msg["content"]) for msg in interaction.response.messages]
    assert any("n1" in text and "неизвестен" not in text and "10%" in text for text in texts)


def test_add_user_modal_posts_admin_api() -> None:
    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del params, headers
        assert method == "POST"
        assert url.endswith("/api/user/add")
        assert isinstance(json, Mapping)
        assert json["user"] == "alice"
        assert json["displayname"] == "Ada"
        return json_ok(msg="Created")

    bot, session = make_admin_bot(handler)
    interaction = FakeInteraction(
        user_id=7,
        data=modal_data(
            "admin:modal:add_user",
            {"username": "alice", "displayname": "Ada", "limit": "10", "days": "0"},
        ),
    )
    run(bot.dispatch_modal(interaction))  # type: ignore[arg-type]
    assert len(session.calls) == 1
    assert "добавлен" in str(interaction.response.messages[-1]["content"]).lower()


def test_permanent_code_posts_unlimited_uses() -> None:
    posted: list[object] = []

    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del method, params, headers
        assert url.endswith("/api/code/add")
        posted.append(json)
        return json_ok(msg="Created")

    bot, session = make_admin_bot(handler)
    bot._pending_codes[7] = {"name": "WELCOME", "type": "register", "days": 30, "gb": 10, "wl_gb": 5}
    interaction = FakeInteraction(user_id=7, data={"custom_id": "admin:code_perma:yes"})
    run(bot.dispatch_component(interaction))  # type: ignore[arg-type]
    assert len(session.calls) == 1
    assert posted == [{
        "code": "WELCOME",
        "action": "register",
        "perma": True,
        "days": 30,
        "gb": 10,
        "wl_gb": 5,
        "uses": -1,
    }]


def test_refresh_abort_reports_counts() -> None:
    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del method, json, params, headers
        assert url.endswith("/api/user/refresh")
        return FakeResponse(
            500,
            {},
            {
                "success": False,
                "msg": "Refresh aborted",
                "obj": {"failed": ["alice"], "aborted": "bob", "succeeded": 1, "total": 3},
            },
        )

    bot, _session = make_admin_bot(handler)
    interaction = FakeInteraction(user_id=7, data={"custom_id": "admin:refresh_all"})
    run(bot.dispatch_component(interaction))  # type: ignore[arg-type]
    text = str(interaction.response.messages[-1]["content"])
    assert "прервано" in text
    assert "Успешно: 1" in text
    assert "панель недоступна: 1" in text
    assert "всего: 3" in text


def test_edit_user_menu_remembers_username() -> None:
    bot, session = make_admin_bot(_ok)
    interaction = FakeInteraction(user_id=7, data={"custom_id": "admin:edit_user:alice"})
    run(bot.dispatch_component(interaction))  # type: ignore[arg-type]
    assert session.calls == []
    assert bot._pending_edits[7] == {"username": "alice"}
    ids = [getattr(item, "custom_id", None) for item in getattr(interaction.response.messages[-1]["view"], "children", [])]
    assert "admin:edit:fp" in ids
    assert "admin:edit:name" in ids

    follow = FakeInteraction(user_id=7, data={"custom_id": "admin:edit:name"})
    run(bot.dispatch_component(follow))  # type: ignore[arg-type]
    assert follow.response.modals
    assert "Сессия истекла" not in str(follow.response.messages)


def test_code_type_keeps_name_and_type() -> None:
    bot, session = make_admin_bot(_ok)
    named = FakeInteraction(
        user_id=7,
        data=modal_data("admin:modal:add_code", {"code": "WELCOME"}),
    )
    run(bot.dispatch_modal(named))  # type: ignore[arg-type]
    assert session.calls == []
    assert bot._pending_codes[7] == {"name": "WELCOME"}

    typed = FakeInteraction(user_id=7, data={"custom_id": "admin:codetype:register"})
    run(bot.dispatch_component(typed))  # type: ignore[arg-type]
    assert bot._pending_codes[7] == {"name": "WELCOME", "type": "register"}
    assert typed.response.modals
    modal = typed.response.modals[-1]
    assert getattr(modal, "custom_id", None) == "admin:modal:code_days"

    days = FakeInteraction(
        user_id=7,
        data=modal_data("admin:modal:code_days", {"days": "30", "gb": "10", "wl_gb": "5"}),
    )
    run(bot.dispatch_modal(days))  # type: ignore[arg-type]
    assert "Сессия истекла" not in str(days.response.messages)
    assert bot._pending_codes[7]["type"] == "register"
    assert bot._pending_codes[7]["name"] == "WELCOME"


def test_edit_modals_open_before_user_info() -> None:
    calls: list[str] = []

    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del method, json, params, headers
        calls.append(url)
        return json_ok(stats_obj())

    bot, _session = make_admin_bot(handler)
    bot._pending_edits[7] = {"username": "alice"}
    for custom_id in ("admin:edit:limit", "admin:edit:wl_limit", "admin:edit:time", "admin:edit:name"):
        interaction = FakeInteraction(user_id=7, data={"custom_id": custom_id})
        run(bot.dispatch_component(interaction))  # type: ignore[arg-type]
        assert interaction.response.modals
        assert interaction.response.deferred is False
    assert calls == []


def test_leaderboard_text_uses_discord_limit() -> None:
    rows = [{"username": f"user-{index:03d}", "amount": 2_000_000_000} for index in range(40)]

    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del method, json, params, headers
        assert url.endswith("/api/leaderboard")
        return json_ok(rows)

    bot, _session = make_admin_bot(handler)
    bot._pending_leaderboard[7] = {"type": "total", "order": "desc"}
    interaction = FakeInteraction(
        user_id=7,
        data=modal_data("admin:modal:lb_window", {"window": "0"}),
    )
    with patch("admin.leaderboard.leaderboard_chart", return_value=None):
        run(bot.dispatch_modal(interaction))  # type: ignore[arg-type]
    text = str(interaction.response.messages[-1]["content"])
    assert "user-000" in text
    assert "user-039" in text
    assert len(text.encode("utf-8")) > 1024
    assert len(text.encode("utf-8")) <= 2000
