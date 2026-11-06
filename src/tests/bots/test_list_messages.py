from __future__ import annotations

from html import unescape
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from xml.etree import ElementTree as ET  # ruff: ignore[suspicious-xml-etree-import] - generated test data

import pytest

import discord
from bots.list_messages import list_messages
from bots.telegram.admin.codes import AdminCodesMixin
from bots.telegram.admin.users import AdminUsersMixin
from tests.support.discord import FakeInteraction, make_bots
from tests.support.telegram import subscription_mock


@pytest.mark.parametrize(("html", "limit"), [(True, 4096), (False, 2000)])
def test_list_messages_boundaries_and_oversized_fields(html: bool, limit: int) -> None:
    header = "<b>List</b>\n\n" if html else "**List**\n\n"
    name = "<&😀" * 3000 if html else "user😀" * 2000
    login = "&😀" * 3000 if html else "login😀" * 2000
    messages = list(list_messages(header, [(name, login), ("last", None)], limit=limit, html=html))
    assert len(messages) > 2
    names: list[str] = []
    logins: list[str] = []
    for message in messages:
        assert len(message.encode("utf-16-le")) // 2 <= limit
        assert message.startswith(header)
        if html:
            root = ET.fromstring(f"<root>{message}</root>")  # ruff: ignore[suspicious-xml-element-tree-usage] - generated test data
            names.extend(element.text or "" for element in root.iter("code"))
        else:
            assert message.count("`") % 2 == 0
            names.extend(message.split("`")[1::2])
        logins.extend(
            unescape(line.split(", логин: ", 1)[1]) if html else line.split(", логин: ", 1)[1]
            for line in message[len(header) :].splitlines()
            if ", логин: " in line
        )
    assert "".join(names) == name + "last"
    assert "".join(logins) == login


@pytest.mark.parametrize("html", [True, False])
def test_short_list_format_unchanged(html: bool) -> None:
    row = (
        "- <code>alice</code>, логин: tg\n- <code>bob</code>"
        if html
        else "- `alice`, логин: tg\n- `bob`"
    )
    assert list(
        list_messages("List\n\n", [("alice", "tg"), ("bob", None)], limit=2000, html=html)
    ) == [
        "List\n\n" + row,
    ]


@pytest.mark.parametrize(("html", "limit"), [(True, 4096), (False, 2000)])
def test_exact_message_limit(html: bool, limit: int) -> None:
    header = "List\n\n"
    wrapper_size = len("- <code></code>") if html else len("- ``")
    name = "x" * (limit - len(header) - wrapper_size)
    messages = list(list_messages(header, [(name, None), ("next", None)], limit=limit, html=html))
    assert len(messages) == 2
    assert len(messages[0]) == limit
    assert name in messages[0]
    assert "next" in messages[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("codes", [True, False])
async def test_telegram_lists_split_and_keep_menus_and_html(codes: bool) -> None:
    mixin = AdminCodesMixin() if codes else AdminUsersMixin()
    sub = subscription_mock()
    mixin.sub = sub
    mixin.log = MagicMock()
    send = AsyncMock()
    mixin._send_message = send  # type: ignore[method-assign]
    entries = [f"entry{i}<&😀" for i in range(700)]
    if isinstance(mixin, AdminCodesMixin):
        mixin.get_codes_menu = MagicMock(return_value="codes")  # type: ignore[method-assign]
        sub.code_svc.list_code.return_value = entries
        await mixin._cb_list_codes(7)
    else:
        mixin.get_main_menu = MagicMock(return_value="main")  # type: ignore[method-assign]
        sub.panel_svc.get_online_users.return_value = dict.fromkeys(entries, "<tg>&")
        await mixin._cb_online_users(7)
    assert send.await_count > 1
    actual: list[str] = []
    for call in send.await_args_list:
        text = call.args[1]
        assert len(text.encode("utf-16-le")) // 2 <= 4096
        root = ET.fromstring(f"<root>{text}</root>")  # ruff: ignore[suspicious-xml-element-tree-usage] - generated test data
        actual.extend(element.text or "" for element in root.iter("code"))
        assert call.kwargs == {"parse_mode": "HTML", "reply_markup": "codes" if codes else "main"}
        assert call.args[0] == 7
    assert actual == entries


@pytest.mark.asyncio
@pytest.mark.parametrize("guild_id", [None, 123])
async def test_discord_online_list_uses_bounded_followups(guild_id: int | None) -> None:
    _, bot, _, sub = make_bots()
    entries = {f"user{i}😀": f"login{i}" for i in range(400)}
    sub.panel_svc = MagicMock(
        get_online_status=AsyncMock(return_value=SimpleNamespace(users=entries))
    )
    fake = FakeInteraction(guild_id=guild_id)
    await bot._cb_online_users(cast(discord.Interaction, fake))
    assert fake.response.deferred
    assert len(fake.response.messages) > 1
    rows: list[str] = []
    for message in fake.response.messages:
        text = str(message["content"])
        assert len(text.encode("utf-16-le")) // 2 <= 2000
        rows.extend(text.split("\n\n", 1)[1].splitlines())
        assert isinstance(message["view"], discord.ui.View)
        assert message.get("ephemeral", False) == (guild_id is not None)
    assert rows == [f"- `{name}`, логин: {login}" for name, login in entries.items()]


def assert_codes_menu(message: dict[str, object]) -> None:
    view = message["view"]
    assert isinstance(view, discord.ui.View)
    assert {item.custom_id for item in view.children if isinstance(item, discord.ui.Button)} == {
        "admin:add_code",
        "admin:del_code",
        "admin:list_codes",
        "admin:info_code",
        "admin:cancel",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("guild_id", [None, 123])
async def test_discord_codes_list_many_long_unicode_names(guild_id: int | None) -> None:
    _, bot, _, sub = make_bots()
    entries = [f"код{i:03d}" + "界😀" * 100 for i in range(70)]
    sub.code_svc.list_code.return_value = entries
    fake = FakeInteraction(guild_id=guild_id)

    await bot._cb_list_codes(cast(discord.Interaction, fake))

    assert fake.response.deferred
    assert len(fake.response.messages) > 1
    rows: list[str] = []
    for message in fake.response.messages:
        text = str(message["content"])
        assert len(text.encode("utf-16-le")) // 2 <= 2000
        assert text.startswith("🎟 **Список кодов:**\n\n")
        rows.extend(text.split("\n\n", 1)[1].splitlines())
        assert_codes_menu(message)
        assert message.get("ephemeral", False) == (guild_id is not None)
    assert rows == [f"- `{name}`" for name in entries]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [1999, 2000, 2001])
async def test_discord_codes_list_exact_utf16_boundaries(size: int) -> None:
    _, bot, _, sub = make_bots()
    header = "🎟 **Список кодов:**\n\n"
    overhead = len((header + "- ``").encode("utf-16-le")) // 2
    name = "😀" + "я" * (size - overhead - 2)
    sub.code_svc.list_code.return_value = [name]
    fake = FakeInteraction()

    await bot._cb_list_codes(cast(discord.Interaction, fake))

    assert len(fake.response.messages) == (1 if size <= 2000 else 2)
    parts: list[str] = []
    for message in fake.response.messages:
        text = str(message["content"])
        assert text.startswith(header)
        assert len(text.encode("utf-16-le")) // 2 <= 2000
        parts.extend(text.split("`")[1::2])
        assert_codes_menu(message)
    assert "".join(parts) == name
    if size <= 2000:
        assert len(str(fake.response.messages[0]["content"]).encode("utf-16-le")) // 2 == size


@pytest.mark.asyncio
async def test_discord_codes_list_oversized_individual_name() -> None:
    _, bot, _, sub = make_bots()
    name = "😀界" * 3000
    sub.code_svc.list_code.return_value = [name, "последний"]
    fake = FakeInteraction()

    await bot._cb_list_codes(cast(discord.Interaction, fake))

    assert len(fake.response.messages) > 2
    parts: list[str] = []
    for message in fake.response.messages:
        text = str(message["content"])
        assert text.startswith("🎟 **Список кодов:**\n\n")
        assert len(text.encode("utf-16-le")) // 2 <= 2000
        parts.extend(text.split("`")[1::2])
        assert_codes_menu(message)
    assert parts[-1] == "последний"
    assert "".join(parts[:-1]) == name


@pytest.mark.asyncio
async def test_discord_codes_list_empty() -> None:
    _, bot, _, sub = make_bots()
    sub.code_svc.list_code.return_value = []
    fake = FakeInteraction()

    await bot._cb_list_codes(cast(discord.Interaction, fake))

    assert fake.response.deferred
    assert len(fake.response.messages) == 1
    assert fake.response.messages[0]["content"] == "Список кодов пуст."
    assert_codes_menu(fake.response.messages[0])
