from __future__ import annotations

import discord

from common import DiscordIOMixin
from composition import PublicFeatureMixin

__all__ = ["PublicCommonMixin"]


class PublicCommonMixin(DiscordIOMixin, PublicFeatureMixin):
    def get_lang(self, user_id: int) -> str:
        lang = self.sessions.lang(user_id)
        return lang if lang in ("en", "ru") else "en"

    def has_lang(self, user_id: int) -> bool:
        return self.sessions.lang(user_id) in ("en", "ru")

    def is_logged_in(self, user_id: int) -> bool:
        return self.sessions.logged_in(user_id)

    def text(self, lang: str, key: str, **kwargs: object) -> str | None:
        table = self.TEXTS.get(lang) or self.TEXTS.get("en")
        if not table:
            return None
        value = table.get(key)
        if not value:
            return None
        if kwargs:
            return value.format(**kwargs)
        return value

    def language_view(self) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="🇷🇺 Русский", custom_id="lang_ru", style=discord.ButtonStyle.secondary))
        view.add_item(discord.ui.Button(label="🇬🇧 English", custom_id="lang_en", style=discord.ButtonStyle.secondary))
        return view

    def main_menu_view(self, lang: str, *, logged_in: bool) -> discord.ui.View:
        t = self.TEXTS[lang]
        view = discord.ui.View(timeout=None)
        if logged_in:
            view.add_item(discord.ui.Button(label=t["btn_main_account"], custom_id="menu_account", style=discord.ButtonStyle.primary))
            view.add_item(discord.ui.Button(label=t["btn_main_sub"], custom_id="menu_sub", style=discord.ButtonStyle.primary))
        else:
            view.add_item(discord.ui.Button(label=t["btn_login"], custom_id="login_credentials", style=discord.ButtonStyle.success))
            view.add_item(discord.ui.Button(label=t.get("btn_register", t["btn_login"]), custom_id="login_register", style=discord.ButtonStyle.success))
        view.add_item(discord.ui.Button(label=t["btn_lang"], custom_id="menu_lang", style=discord.ButtonStyle.secondary))
        view.add_item(discord.ui.Button(label=t["btn_support"], custom_id="menu_support", style=discord.ButtonStyle.secondary))
        return view

    def subscription_menu_view(self, lang: str) -> discord.ui.View:
        t = self.TEXTS[lang]
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label=t["btn_info"], custom_id="menu_info", style=discord.ButtonStyle.secondary))
        view.add_item(discord.ui.Button(label=t["btn_get_sub"], custom_id="menu_get_sub", style=discord.ButtonStyle.secondary))
        view.add_item(discord.ui.Button(label=t["btn_bonus"], custom_id="menu_bonus", style=discord.ButtonStyle.secondary))
        view.add_item(discord.ui.Button(label=t["btn_reset"], custom_id="menu_reset", style=discord.ButtonStyle.danger))
        view.add_item(discord.ui.Button(label=t["btn_chart"], custom_id="menu_chart", style=discord.ButtonStyle.secondary))
        view.add_item(discord.ui.Button(label=t["btn_main_back"], custom_id="menu_main", style=discord.ButtonStyle.primary))
        return view

    def account_menu_view(self, lang: str) -> discord.ui.View:
        t = self.TEXTS[lang]
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label=t["btn_settings"], custom_id="menu_settings", style=discord.ButtonStyle.secondary))
        view.add_item(discord.ui.Button(label=t["btn_logout"], custom_id="menu_logout", style=discord.ButtonStyle.danger))
        view.add_item(discord.ui.Button(label=t["btn_help"], custom_id="menu_help", style=discord.ButtonStyle.secondary))
        view.add_item(discord.ui.Button(label=t["btn_delete"], custom_id="menu_delete", style=discord.ButtonStyle.danger))
        view.add_item(discord.ui.Button(label=t["btn_main_back"], custom_id="menu_main", style=discord.ButtonStyle.primary))
        return view

    def confirm_view(self, lang: str, action: str) -> discord.ui.View:
        t = self.TEXTS[lang]
        view = discord.ui.View(timeout=None)
        confirm_label = t.get("btn_confirm", "✅")
        view.add_item(discord.ui.Button(label=confirm_label, custom_id=f"confirm_{action}", style=discord.ButtonStyle.danger))
        view.add_item(discord.ui.Button(label=t["btn_cancel"], custom_id="confirm_cancel", style=discord.ButtonStyle.secondary))
        return view

    def in_dm(self, interaction: discord.Interaction) -> bool:
        channel = interaction.channel
        if interaction.guild_id is None:
            return True
        return isinstance(channel, discord.DMChannel)

    async def require_login(self, interaction: discord.Interaction) -> bool:
        if self.is_logged_in(interaction.user.id):
            return True
        await self._reply_key(interaction, "not_logged_in")
        return False

    async def cmd_start(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        if not self.has_lang(uid):
            await self._respond(
                interaction,
                "Welcome! Please choose your language:\nДобро пожаловать! Выберите язык:",
                view=self.language_view(),
            )
            return
        lang = self.get_lang(uid)
        key = "welcome_reg" if self.is_logged_in(uid) else "welcome_new"
        await self._reply_key(
            interaction,
            key,
            view=self.main_menu_view(lang, logged_in=self.is_logged_in(uid)),
        )

    async def cmd_support(self, interaction: discord.Interaction) -> None:
        await self._reply_key(interaction, "support_text")

    async def set_lang_callback(self, interaction: discord.Interaction, lang: str) -> None:
        if lang not in ("en", "ru"):
            return
        self.sessions.set_lang(interaction.user.id, lang)
        await self._reply_key(
            interaction,
            "lang_set",
            view=self.main_menu_view(lang, logged_in=self.is_logged_in(interaction.user.id)),
        )
