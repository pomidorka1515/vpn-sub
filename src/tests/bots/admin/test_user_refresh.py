from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from bots.telegram.admin.users import AdminUsersMixin
from errors import PanelUnavailableError
from tests.support.telegram import subscription_mock


@pytest.fixture
def refresh() -> tuple[AdminUsersMixin, MagicMock]:
    mixin = AdminUsersMixin.__new__(AdminUsersMixin)
    subscription = subscription_mock()
    mixin.sub = subscription
    mixin.log = MagicMock()
    mixin.get_main_menu = MagicMock(return_value="menu")  # type: ignore[method-assign]
    mixin._send_message = AsyncMock()  # type: ignore[method-assign]
    return mixin, subscription


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcomes", "attempted", "successes", "panel_failures", "unattempted"),
    [
        ([RuntimeError("boom")], ["alice"], 0, 0, 2),
        ([None, RuntimeError("boom")], ["alice", "bob"], 1, 0, 1),
        ([PanelUnavailableError("down"), RuntimeError("boom")], ["alice", "bob"], 0, 1, 1),
        (
            [None, PanelUnavailableError("down"), RuntimeError("boom")],
            ["alice", "bob", "carol"],
            1,
            1,
            0,
        ),
    ],
)
async def test_refresh_abort_reports_actual_outcomes(
    refresh: tuple[AdminUsersMixin, MagicMock],
    outcomes: list[Exception | None],
    attempted: list[str],
    successes: int,
    panel_failures: int,
    unattempted: int,
) -> None:
    mixin, subscription = refresh
    subscription.user_svc.list_users.return_value = ["alice", "bob", "carol"]
    subscription.business_svc.add_users.side_effect = outcomes

    await mixin._cb_refresh(7)

    assert subscription.business_svc.add_users.await_args_list == [call(name) for name in attempted]
    cast(AsyncMock, mixin._send_message).assert_awaited_once_with(
        7,
        f"❌ Обновление прервано. Успешно: {successes}; "
        f"панель недоступна: {panel_failures}; другая ошибка: 1; "
        f"не обработано: {unattempted}; всего: 3.",
        reply_markup="menu",
    )
    cast(MagicMock, mixin.log).critical.assert_called_once()


@pytest.mark.asyncio
async def test_refresh_continues_after_panel_failure(
    refresh: tuple[AdminUsersMixin, MagicMock],
) -> None:
    mixin, subscription = refresh
    subscription.user_svc.list_users.return_value = ["alice", "bob", "carol"]
    subscription.business_svc.add_users.side_effect = [None, PanelUnavailableError("down"), None]

    await mixin._cb_refresh(7)

    assert subscription.business_svc.add_users.await_args_list == [
        call("alice"),
        call("bob"),
        call("carol"),
    ]
    cast(AsyncMock, mixin._send_message).assert_awaited_once_with(
        7, "⚠️ Обновление завершено с ошибками: bob", reply_markup="menu"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("users", [[], ["alice", "bob"]])
async def test_refresh_success_message_is_unchanged(
    refresh: tuple[AdminUsersMixin, MagicMock], users: list[str]
) -> None:
    mixin, subscription = refresh
    subscription.user_svc.list_users.return_value = users

    await mixin._cb_refresh(7)

    assert subscription.business_svc.add_users.await_args_list == [call(name) for name in users]
    cast(AsyncMock, mixin._send_message).assert_awaited_once_with(
        7, "✅ Все пользователи успешно обновлены.", reply_markup="menu"
    )
