from __future__ import annotations

from public.routing import PublicRoutingMixin
from discord_helpers import FakeUser, run


def test_unauthenticated_users_cannot_hit_protected_handlers() -> None:
    mixin = PublicRoutingMixin()
    assert mixin.command_requires_auth("info")
    assert mixin.command_requires_auth("sub")
    assert mixin.command_requires_auth("delete")
    assert not mixin.command_requires_auth("login")
    assert not mixin.command_requires_auth("start")

    replies: list[str] = []

    class FakeInteraction:
        user = FakeUser(42)
        data: dict[str, object] = {"custom_id": "menu_info"}
        type = None

    async def reply_key(interaction: object, key: str, **kwargs: object) -> None:
        del interaction, kwargs
        replies.append(key)

    mixin.is_logged_in = lambda user_id: False  # type: ignore[method-assign]
    setattr(mixin, "_reply_key", reply_key)
    run(mixin.dispatch_component(FakeInteraction()))  # type: ignore[arg-type]
    assert replies == ["not_logged_in"]

    replies.clear()
    FakeInteraction.data = {"custom_id": "menu_get_sub"}
    run(mixin.dispatch_component(FakeInteraction()))  # type: ignore[arg-type]
    assert replies == ["not_logged_in"]

    replies.clear()
    FakeInteraction.data = {"custom_id": "delete_modal"}
    run(mixin.dispatch_modal(FakeInteraction()))  # type: ignore[arg-type]
    assert replies == ["not_logged_in"]
