from __future__ import annotations

from pathlib import Path
from typing import Any

from discord_helpers import FakeInteraction, FakeUser, json_ok, make_public_bot, run
from public.routing import PublicRoutingMixin


def _invoke_slash(command: Any, interaction: FakeInteraction) -> None:
    callback = command._callback
    binding = getattr(command, "binding", None)
    if binding is not None:
        run(callback(binding, interaction))
    else:
        run(callback(interaction))


def test_unauthenticated_users_cannot_hit_protected_handlers() -> None:
    mixin = PublicRoutingMixin()
    assert mixin.command_requires_auth("info")
    assert mixin.command_requires_auth("sub")
    assert mixin.command_requires_auth("delete")
    assert not mixin.command_requires_auth("login")
    assert not mixin.command_requires_auth("start")

    replies: list[str] = []

    class Interaction:
        user = FakeUser(42)
        data: dict[str, object] = {"custom_id": "menu_info"}
        type = None

    async def reply_key(interaction: object, key: str, **kwargs: object) -> None:
        del interaction, kwargs
        replies.append(key)

    mixin.is_logged_in = lambda user_id: False  # type: ignore[method-assign]
    setattr(mixin, "_reply_key", reply_key)
    run(mixin.dispatch_component(Interaction()))  # type: ignore[arg-type]
    assert replies == ["not_logged_in"]

    replies.clear()
    Interaction.data = {"custom_id": "menu_get_sub"}
    run(mixin.dispatch_component(Interaction()))  # type: ignore[arg-type]
    assert replies == ["not_logged_in"]

    replies.clear()
    Interaction.data = {"custom_id": "delete_modal"}
    run(mixin.dispatch_modal(Interaction()))  # type: ignore[arg-type]
    assert replies == ["not_logged_in"]


def test_slash_wrapper_uses_command_requires_auth(tmp_path: Path) -> None:
    bot, store, _session = make_public_bot(tmp_path, lambda *args: json_ok()) # pyright: ignore
    store.clear_token(7)
    try:
        info_cmd = bot.tree.get_command("info")
        login_cmd = bot.tree.get_command("login")
        assert info_cmd is not None
        assert login_cmd is not None
        assert bot.command_requires_auth("info")
        assert not bot.command_requires_auth("login")

        info = FakeInteraction()
        _invoke_slash(info_cmd, info)
        assert not info.response.deferred
        assert "not logged in" in str(info.response.messages[-1]["content"]).lower()

        login = FakeInteraction()
        _invoke_slash(login_cmd, login)
        assert login.response.modals
        texts = [str(msg.get("content", "")).lower() for msg in login.response.messages]
        assert all("not logged in" not in text for text in texts)
    finally:
        store.close()


def test_unauthenticated_menu_main_and_confirm_cancel_are_public() -> None:
    mixin = PublicRoutingMixin()
    replies: list[str] = []
    started: list[str] = []

    class Interaction:
        user = FakeUser(42)
        data: dict[str, object] = {"custom_id": "menu_main"}
        type = None

    async def reply_key(interaction: object, key: str, **kwargs: object) -> None:
        del interaction, kwargs
        replies.append(key)

    async def cmd_start(interaction: object) -> None:
        del interaction
        started.append("start")

    mixin.is_logged_in = lambda user_id: False  # type: ignore[method-assign]
    setattr(mixin, "_reply_key", reply_key)
    mixin.cmd_start = cmd_start  # type: ignore[method-assign]

    run(mixin.dispatch_component(Interaction()))  # type: ignore[arg-type]
    assert started == ["start"]
    assert "not_logged_in" not in replies

    replies.clear()
    Interaction.data = {"custom_id": "confirm_cancel"}
    run(mixin.dispatch_component(Interaction()))  # type: ignore[arg-type]
    assert replies == ["cancelled"]
