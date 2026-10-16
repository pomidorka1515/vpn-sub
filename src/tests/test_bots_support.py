from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from bot_helpers import telegram_mock
from discord_helpers import FakeInteraction, make_bots
from helpers import config_mock, subscription_config
from telebot import types

import discord
from bots.telegram.public.text_routing import PublicTextRoutingMixin

if TYPE_CHECKING:
    from config.documents import SupportTextConfig


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["en", "ru"])
@pytest.mark.parametrize(
    "overrides",
    [
        None,
        {},
        {"discord": ["Discord EN {literal}", "Discord RU"]},
        {"telegram": ["<b>Telegram EN</b>", "Telegram RU"]},
        {
            "discord": ["Discord EN {literal}", "Discord RU"],
            "telegram": ["<b>Telegram EN</b>", "Telegram RU"],
        },
    ],
)
async def test_support_platform_overrides_and_fallbacks(
    lang: str, overrides: SupportTextConfig | None,
) -> None:
    document = subscription_config()
    if overrides is not None:
        document["support_text"] = overrides
    cfg = config_mock(document)
    telegram = telegram_mock()
    tg = PublicTextRoutingMixin.__new__(PublicTextRoutingMixin)
    tg.cfg = cfg
    tg.bot = telegram
    tg.TEXTS = {lang: {"support_text": f"Telegram fallback {lang}"}}
    message = cast(types.Message, SimpleNamespace(chat=SimpleNamespace(id=7)))
    dc, _, store, _ = make_bots()
    dc.cfg = cfg
    store.set_lang(7, lang)
    index = 0 if lang == "en" else 1
    try:
        await tg._handle_support(message, 7, lang)
        pair = (overrides or {}).get("telegram")
        expected = pair[index] if pair is not None else f"Telegram fallback {lang}"
        telegram.send_message.assert_awaited_once_with(7, expected, parse_mode="HTML")

        fake = FakeInteraction(guild_id=123)
        await dc.cmd_support(cast(discord.Interaction, fake))
        pair = (overrides or {}).get("discord")
        expected = pair[index] if pair is not None else dc.TEXTS[lang]["support_text"]
        assert fake.response.messages[-1]["content"] == expected
        assert fake.response.messages[-1]["ephemeral"] is True

        # Subsequent requests use new config values without recreating either bot.
        document["support_text"] = {
            "telegram": ["Updated TG EN", "Updated TG RU"],
            "discord": ["Updated DC EN", "Updated DC RU"],
        }
        await tg._handle_support(message, 7, lang)
        assert telegram.send_message.call_args.args[1] == document["support_text"]["telegram"][index]
        fake = FakeInteraction()
        await dc.cmd_support(cast(discord.Interaction, fake))
        assert fake.response.messages[-1]["content"] == document["support_text"]["discord"][index]

        del document["support_text"]
        await tg._handle_support(message, 7, lang)
        assert telegram.send_message.call_args.args[1] == f"Telegram fallback {lang}"
        fake = FakeInteraction()
        await dc.cmd_support(cast(discord.Interaction, fake))
        assert fake.response.messages[-1]["content"] == dc.TEXTS[lang]["support_text"]
    finally:
        await dc.rendering.close()
