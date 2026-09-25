from __future__ import annotations

from typing import Self

import discord

from composition import PublicFeatureMixin

__all__ = ["PublicAccountMixin"]


class BonusModal(discord.ui.Modal):
    def __init__(self, title: str, label: str) -> None:
        super().__init__(title=title, custom_id="bonus_modal")
        self.code: discord.ui.TextInput[Self] = discord.ui.TextInput(label=label, custom_id="code", min_length=1, max_length=64)
        self.add_item(self.code)


class DeleteModal(discord.ui.Modal):
    def __init__(self, title: str, label: str) -> None:
        super().__init__(title=title, custom_id="delete_modal")
        self.password: discord.ui.TextInput[Self] = discord.ui.TextInput(
            label=label,
            custom_id="current_password",
            min_length=1,
            max_length=128,
        )
        self.add_item(self.password)


class PublicAccountMixin(PublicFeatureMixin):
    async def cmd_bonus(self, interaction: discord.Interaction) -> None:
        lang = self.get_lang(interaction.user.id)
        t = self.TEXTS[lang]
        modal = BonusModal(title=t.get("btn_bonus", "Bonus"), label=t.get("enter_bonus", "Code"))
        try:
            await interaction.response.send_modal(modal)
        except Exception:
            self.log.error("failed to open bonus modal", exc_info=True)

    async def handle_bonus_modal(self, interaction: discord.Interaction) -> None:
        if not await self.require_login(interaction):
            return
        values = self.modal_values(interaction)
        code = values.get("code", "").strip()
        token = self.sessions.token(interaction.user.id)
        if not token:
            await self._reply_key(interaction, "not_logged_in")
            return
        await self._defer(interaction)
        result = await self.http.bonus(token, code)
        if not await self.consume_result(interaction, result):
            return
        await self._reply_key(interaction, "bonus_success")
        await self.send_info(interaction)

    async def cmd_logout(self, interaction: discord.Interaction) -> None:
        lang = self.get_lang(interaction.user.id)
        warning = self.text(lang, "logout_web_warning")
        await self._respond(
            interaction,
            warning,
            view=self.confirm_view(lang, "logout"),
        )

    async def cmd_reset(self, interaction: discord.Interaction) -> None:
        lang = self.get_lang(interaction.user.id)
        await self._reply_key(
            interaction,
            "confirm_reset",
            view=self.confirm_view(lang, "reset"),
        )

    async def cmd_delete(self, interaction: discord.Interaction) -> None:
        lang = self.get_lang(interaction.user.id)
        t = self.TEXTS[lang]
        modal = DeleteModal(
            title=t.get("btn_delete", "Delete"),
            label=t.get("delete_need_password", t.get("enter_pass", "Password")),
        )
        try:
            await interaction.response.send_modal(modal)
        except Exception:
            self.log.error("failed to open delete modal", exc_info=True)

    async def confirm_logout(self, interaction: discord.Interaction) -> None:
        token = self.sessions.token(interaction.user.id)
        await self._defer(interaction)
        if token:
            result = await self.http.logout(token)
            if result.status not in (200, 401) and not result.ok:
                if not await self.consume_result(interaction, result):
                    return
        self.sessions.clear_token(interaction.user.id)
        lang = self.get_lang(interaction.user.id)
        await self._reply_key(
            interaction,
            "logout_success",
            view=self.main_menu_view(lang, logged_in=False),
        )

    async def confirm_reset(self, interaction: discord.Interaction) -> None:
        token = self.sessions.token(interaction.user.id)
        if not token:
            await self._reply_key(interaction, "not_logged_in")
            return
        await self._defer(interaction)
        result = await self.http.reset(token)
        if result.status == 429:
            await self._reply_key(interaction, "rate_limited")
            return
        if result.status == 401:
            self.sessions.clear_token(interaction.user.id)
            await self._reply_key(interaction, "session_expired")
            return
        if not result.ok:
            if result.msg:
                await self._respond(interaction, result.msg)
            return
        self.sessions.clear_token(interaction.user.id)
        lang = self.get_lang(interaction.user.id)
        await self._reply_key(
            interaction,
            "reset_success",
            view=self.main_menu_view(lang, logged_in=False),
        )

    async def handle_delete_modal(self, interaction: discord.Interaction) -> None:
        values = self.modal_values(interaction)
        password = values.get("current_password", "")
        token = self.sessions.token(interaction.user.id)
        if not token:
            await self._reply_key(interaction, "not_logged_in")
            return
        await self._defer(interaction)
        result = await self.http.delete(token, password)
        if result.status == 401:
            if result.msg and "password" in result.msg.lower():
                await self._respond(interaction, result.msg)
                return
            self.sessions.clear_token(interaction.user.id)
            await self._reply_key(interaction, "session_expired")
            return
        if not await self.consume_result(interaction, result):
            return
        self.sessions.clear_token(interaction.user.id)
        lang = self.get_lang(interaction.user.id)
        await self._reply_key(
            interaction,
            "delete_success",
            view=self.main_menu_view(lang, logged_in=False),
        )
