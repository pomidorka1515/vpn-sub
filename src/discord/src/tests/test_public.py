from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from discord_helpers import (
    FakeInteraction,
    FakeResponse,
    FakeSession,
    json_ok,
    make_public_bot,
    modal_data,
    run,
    stats_obj,
)
from public.bot import PublicBot
from sessions import SessionStore

Handler = Callable[..., Any]
PublicBotFactory = Callable[[Handler], tuple[PublicBot, SessionStore, FakeSession]]


@pytest.fixture
def public_bot_factory(tmp_path: Path) -> Iterator[PublicBotFactory]:
    stores: list[SessionStore] = []

    def factory(handler: Handler) -> tuple[PublicBot, SessionStore, FakeSession]:
        bot, store, session = make_public_bot(tmp_path, handler)
        stores.append(store)
        return bot, store, session

    yield factory
    for store in stores:
        store.close()


def test_info_uses_stats(public_bot_factory: PublicBotFactory) -> None:
    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del method, json, params
        assert url.endswith("/stats")
        assert headers == {"Cookie": "auth_token=tok"}
        return json_ok(stats_obj())

    bot, _store, session = public_bot_factory(handler)
    interaction = FakeInteraction()
    run(bot.cmd_info(interaction))  # type: ignore[arg-type]
    assert interaction.response.deferred
    assert len(session.calls) == 1
    content = str(interaction.response.messages[-1]["content"])
    assert "alice" in content
    assert "chrome" in content


def test_sub_uses_stats_and_qr(public_bot_factory: PublicBotFactory) -> None:
    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del method, json, headers
        if url.endswith("/stats"):
            return json_ok(stats_obj())
        if url.endswith("/qr"):
            assert params == {"lang": "en"}
            return FakeResponse(200, {}, body=b"qr-png", content_type="image/png")
        raise AssertionError(url)

    bot, _store, session = public_bot_factory(handler)
    interaction = FakeInteraction(guild_id=None)
    run(bot.cmd_sub(interaction))  # type: ignore[arg-type]
    paths = [str(call["url"]) for call in session.calls]
    assert paths[0].endswith("/stats")
    assert paths[1].endswith("/qr")
    last = interaction.response.messages[-1]
    assert "subscription is ready" in str(last["content"])
    assert last.get("file") is not None
    assert last.get("view") is not None


def test_bonus_posts_code_then_refreshes_info(public_bot_factory: PublicBotFactory) -> None:
    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del method, params, headers
        if url.endswith("/bonus"):
            assert json == {"code": "BONUS1"}
            return json_ok()
        if url.endswith("/stats"):
            return json_ok(stats_obj())
        raise AssertionError(url)

    bot, _store, session = public_bot_factory(handler)
    interaction = FakeInteraction(data=modal_data("bonus_modal", {"code": "BONUS1"}))
    run(bot.handle_bonus_modal(interaction))  # type: ignore[arg-type]
    paths = [str(call["url"]) for call in session.calls]
    assert paths[0].endswith("/bonus")
    assert paths[1].endswith("/stats")
    texts = [str(msg["content"]) for msg in interaction.response.messages]
    assert any("successfully applied" in text.lower() or "bonus" in text.lower() for text in texts)
    assert any("alice" in text for text in texts)


def test_settings_posts_name(public_bot_factory: PublicBotFactory) -> None:
    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del method, params, headers
        assert url.endswith("/settings")
        assert json == {"name": "Ada"}
        return json_ok()

    bot, _store, session = public_bot_factory(handler)
    interaction = FakeInteraction(data=modal_data("settings_name_modal", {"name": "Ada"}))
    run(bot.handle_name_modal(interaction))  # type: ignore[arg-type]
    assert len(session.calls) == 1
    content = str(interaction.response.messages[-1]["content"])
    assert "Display name changed" in content


def test_chart_uses_history_and_stats(public_bot_factory: PublicBotFactory) -> None:
    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del method, json, headers
        if url.endswith("/history"):
            assert params == {"days": 14}
            return json_ok([{"ts": 1, "up": 10, "down": 20, "wl_up": 1, "wl_down": 2}])
        if url.endswith("/stats"):
            return json_ok(stats_obj())
        raise AssertionError(url)

    bot, _store, session = public_bot_factory(handler)
    interaction = FakeInteraction()

    async def fake_to_thread(func: Any, *args: object, **kwargs: object) -> object:
        del func, args, kwargs
        return None

    with patch("public.traffic.asyncio.to_thread", fake_to_thread):
        run(bot.render_chart(interaction, 14))  # type: ignore[arg-type]
    paths = [str(call["url"]) for call in session.calls]
    assert paths[0].endswith("/history")
    assert paths[1].endswith("/stats")
    last = interaction.response.messages[-1]
    assert "14" in str(last["content"])


def test_logout_clears_session(public_bot_factory: PublicBotFactory) -> None:
    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del method, json, params, headers
        assert url.endswith("/logout")
        return json_ok()

    bot, store, session = public_bot_factory(handler)
    interaction = FakeInteraction()
    run(bot.confirm_logout(interaction))  # type: ignore[arg-type]
    assert len(session.calls) == 1
    assert not store.logged_in(7)
    assert "logged out" in str(interaction.response.messages[-1]["content"]).lower()


def test_reset_clears_session(public_bot_factory: PublicBotFactory) -> None:
    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del method, json, params, headers
        assert url.endswith("/reset")
        return json_ok()

    bot, store, session = public_bot_factory(handler)
    interaction = FakeInteraction()
    run(bot.confirm_reset(interaction))  # type: ignore[arg-type]
    assert len(session.calls) == 1
    assert not store.logged_in(7)
    assert "reset" in str(interaction.response.messages[-1]["content"]).lower()


def test_delete_posts_password_and_clears_session(public_bot_factory: PublicBotFactory) -> None:
    def handler(method: str, url: str, json: object, params: object, headers: object) -> FakeResponse:
        del method, params, headers
        assert url.endswith("/delete")
        assert json == {"current_password": "secret"}
        return json_ok()

    bot, store, session = public_bot_factory(handler)
    interaction = FakeInteraction(data=modal_data("delete_modal", {"current_password": "secret"}))
    run(bot.handle_delete_modal(interaction))  # type: ignore[arg-type]
    assert len(session.calls) == 1
    assert not store.logged_in(7)
    assert "deleted" in str(interaction.response.messages[-1]["content"]).lower()



def test_slash_commands_sync_in_setup_hook_not_on_ready(public_bot_factory: PublicBotFactory) -> None:
    bot, _store, _session = public_bot_factory(lambda *args: json_ok())
    calls = {"n": 0}

    async def fake_sync() -> list[object]:
        calls["n"] += 1
        return []

    bot.tree.sync = fake_sync  # type: ignore[assignment, method-assign]
    run(bot.bot.setup_hook())
    assert calls["n"] == 1
    on_ready = getattr(bot.bot, "on_ready")
    run(on_ready())
    assert calls["n"] == 1
