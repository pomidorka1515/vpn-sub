from __future__ import annotations

from html.parser import HTMLParser
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from telebot import types

from bots.telegram.admin.users import AdminUsersMixin
from tests.support.telegram import subscription_mock, telegram_mock


class CodeCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_code = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "code":
            assert not self.in_code
            self.in_code = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "code":
            assert self.in_code
            self.in_code = False

    def handle_data(self, data: str) -> None:
        if self.in_code:
            self.parts.append(data)


def _buttons(markup: object) -> list[str]:
    return [
        button.callback_data
        for row in cast(types.InlineKeyboardMarkup, markup).inline_keyboard
        for button in row
        if button.callback_data is not None
    ]


@pytest.fixture
def user_list() -> tuple[AdminUsersMixin, MagicMock]:
    mixin = AdminUsersMixin.__new__(AdminUsersMixin)
    mixin.sub = subscription_mock()
    mixin.bot = telegram_mock()
    mixin.USERS_PER_PAGE = 10
    mixin._send_message = AsyncMock()  # type: ignore[method-assign]
    return mixin, cast(MagicMock, mixin._send_message)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "username",
    ["x" * 9000, "😀<&" * 3000, "😀" * 2200],
    ids=["long-ascii", "escaped-unicode", "utf16-limit"],
)
async def test_long_existing_username_is_split_safely_with_navigation(
    user_list: tuple[AdminUsersMixin, MagicMock], username: str
) -> None:
    mixin, send = user_list
    before = [f"before{i}" for i in range(10)]
    after = [f"after{i}" for i in range(10)]
    cast(MagicMock, mixin.sub.user_svc.list_users).return_value = [*before, username, *after]

    await mixin._cb_list_users(7, page=1)

    assert send.await_count > 1
    parser = CodeCollector()
    for call in send.await_args_list:
        text = call.args[1]
        assert call.args[0] == 7
        assert len(text.encode("utf-16-le")) // 2 <= 4096
        assert call.kwargs["parse_mode"] == "HTML"
        assert _buttons(call.kwargs["reply_markup"]) == [
            "page_list_users_0",
            "page_list_users_2",
            "noop",
            "cancel",
        ]
        parser.feed(text)
        assert not parser.in_code
    parser.close()
    assert "".join(parser.parts) == username + "".join(after[:9])


@pytest.mark.asyncio
async def test_normal_pages_keep_rows_and_navigation(
    user_list: tuple[AdminUsersMixin, MagicMock],
) -> None:
    mixin, send = user_list
    names = [f"user{i}" for i in range(21)]
    cast(MagicMock, mixin.sub.user_svc.list_users).return_value = names

    for page, expected_buttons in [
        (0, ["page_list_users_1", "noop", "cancel"]),
        (1, ["page_list_users_0", "page_list_users_2", "noop", "cancel"]),
        (99, ["page_list_users_1", "noop", "cancel"]),
    ]:
        send.reset_mock()
        await mixin._cb_list_users(7, page=page)
        send.assert_awaited_once()
        call = send.await_args
        assert len(call.args[1].encode("utf-16-le")) // 2 <= 4096
        assert _buttons(call.kwargs["reply_markup"]) == expected_buttons
        parser = CodeCollector()
        parser.feed(call.args[1])
        parser.close()
        actual_page = min(page, 2)
        assert parser.parts == names[actual_page * 10 : (actual_page + 1) * 10]
