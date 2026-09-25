from __future__ import annotations

from typing import Self, cast

import discord

from composition import PublicFeatureMixin

__all__ = ["PublicSettingsMixin"]


class NameModal(discord.ui.Modal):
    def __init__(self, title: str, label: str) -> None:
        super().__init__(title=title, custom_id="settings_name_modal")
        self.name: discord.ui.TextInput[Self] = discord.ui.TextInput(label=label, custom_id="name", min_length=1, max_length=16)
        self.add_item(self.name)


class LoginChangeModal(discord.ui.Modal):
    def __init__(self, title: str, login_label: str, password_label: str) -> None:
        super().__init__(title=title, custom_id="settings_login_modal")
        self.username: discord.ui.TextInput[Self] = discord.ui.TextInput(label=login_label, custom_id="username", min_length=1, max_length=32)
        self.current_password: discord.ui.TextInput[Self] = discord.ui.TextInput(
            label=password_label,
            custom_id="current_password",
            min_length=1,
            max_length=128,
        )
        self.add_item(self.username)
        self.add_item(self.current_password)


class PasswordChangeModal(discord.ui.Modal):
    def __init__(self, title: str, new_label: str, current_label: str) -> None:
        super().__init__(title=title, custom_id="settings_pass_modal")
        self.password: discord.ui.TextInput[Self] = discord.ui.TextInput(label=new_label, custom_id="password", min_length=1, max_length=128)
        self.current_password: discord.ui.TextInput[Self] = discord.ui.TextInput(
            label=current_label,
            custom_id="current_password",
            min_length=1,
            max_length=128,
        )
        self.add_item(self.password)
        self.add_item(self.current_password)


class PublicSettingsMixin(PublicFeatureMixin):
    def settings_menu_view(self, lang: str) -> discord.ui.View:
        t = self.TEXTS[lang]
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label=t["name_label"], custom_id="set_name", style=discord.ButtonStyle.secondary))
        view.add_item(discord.ui.Button(label=t["fp_label"], custom_id="set_fp", style=discord.ButtonStyle.secondary))
        view.add_item(discord.ui.Button(label=t["login_label"], custom_id="set_login", style=discord.ButtonStyle.secondary))
        view.add_item(discord.ui.Button(label=t["pass_label"], custom_id="set_pass", style=discord.ButtonStyle.secondary))
        return view

    async def cmd_settings(self, interaction: discord.Interaction) -> None:
        lang = self.get_lang(interaction.user.id)
        await self._reply_key(interaction, "settings_menu", view=self.settings_menu_view(lang))

    async def open_name_modal(self, interaction: discord.Interaction) -> None:
        lang = self.get_lang(interaction.user.id)
        t = self.TEXTS[lang]
        modal = NameModal(title=t.get("name_label", "Name"), label=t.get("settings_name_prompt", "Name"))
        try:
            await interaction.response.send_modal(modal)
        except Exception:
            self.log.error("failed to open name modal", exc_info=True)

    async def open_login_modal(self, interaction: discord.Interaction) -> None:
        lang = self.get_lang(interaction.user.id)
        t = self.TEXTS[lang]
        modal = LoginChangeModal(
            title=t.get("login_label", "Login"),
            login_label=t.get("settings_login_prompt", "Login"),
            password_label=t.get("enter_pass", "Current password"),
        )
        try:
            await interaction.response.send_modal(modal)
        except Exception:
            self.log.error("failed to open login modal", exc_info=True)

    async def open_pass_modal(self, interaction: discord.Interaction) -> None:
        lang = self.get_lang(interaction.user.id)
        t = self.TEXTS[lang]
        modal = PasswordChangeModal(
            title=t.get("pass_label", "Password"),
            new_label=t.get("settings_pass_prompt", "New password"),
            current_label=t.get("enter_pass", "Current password"),
        )
        try:
            await interaction.response.send_modal(modal)
        except Exception:
            self.log.error("failed to open password modal", exc_info=True)

    async def open_fingerprint_menu(self, interaction: discord.Interaction) -> None:
        token = self.sessions.token(interaction.user.id)
        if not token:
            await self._reply_key(interaction, "not_logged_in")
            return
        await self._defer(interaction)
        result = await self.http.fingerprints(token)
        if not await self.consume_result(interaction, result):
            return
        raw_fps = result.obj
        if isinstance(raw_fps, list):
            fps = cast(list[str], raw_fps)
        else:
            fps = []
        options: list[discord.SelectOption] = []
        for item in fps:
            options.append(discord.SelectOption(label=item, value=item))
        if not options:
            await self._reply_key(interaction, "bad_response")
            return
        view = discord.ui.View(timeout=None)
        select: discord.ui.Select[discord.ui.View] = discord.ui.Select(
            custom_id="fp_select",
            placeholder=self.TEXTS[self.get_lang(interaction.user.id)].get("settings_fp_prompt", "Fingerprint"),
            options=options[:25],
        )
        view.add_item(select)
        await self._reply_key(interaction, "settings_fp_prompt", view=view)

    async def apply_fingerprint(self, interaction: discord.Interaction, fingerprint: str) -> None:
        token = self.sessions.token(interaction.user.id)
        if not token:
            await self._reply_key(interaction, "not_logged_in")
            return
        await self._defer(interaction)
        result = await self.http.settings(token, fingerprint=fingerprint)
        if not await self.consume_result(interaction, result):
            return
        await self._reply_key(interaction, "settings_fp_success")

    async def handle_name_modal(self, interaction: discord.Interaction) -> None:
        values = self.modal_values(interaction)
        name = values.get("name", "").strip()
        lang = self.get_lang(interaction.user.id)
        if len(name) > 16:
            text = self.text(lang, "length_displayname", ln=16)
            if text:
                await self._respond(interaction, text)
            return
        token = self.sessions.token(interaction.user.id)
        if not token:
            await self._reply_key(interaction, "not_logged_in")
            return
        await self._defer(interaction)
        result = await self.http.settings(token, name=name)
        if not await self.consume_result(interaction, result):
            return
        await self._reply_key(interaction, "settings_name_success")

    async def handle_login_change_modal(self, interaction: discord.Interaction) -> None:
        values = self.modal_values(interaction)
        username = values.get("username", "").strip()
        current_password = values.get("current_password", "")
        token = self.sessions.token(interaction.user.id)
        if not token:
            await self._reply_key(interaction, "not_logged_in")
            return
        await self._defer(interaction)
        check = await self.http.validate_username(username)
        if not await self.consume_result(interaction, check):
            return
        obj = check.obj
        if not isinstance(obj, dict):
            await self._reply_key(interaction, "bad_response")
            return
        obj = cast(dict[str, object], obj)
        if not obj.get("valid"):
            await self._reply_key(interaction, "length_username", ln=32)
            return
        if obj.get("taken"):
            await self._reply_key(interaction, "username_taken")
            return
        result = await self.http.settings(token, username=username, current_password=current_password)
        if not await self.consume_result(interaction, result):
            return
        await self._reply_key(interaction, "settings_login_success")

    async def handle_pass_change_modal(self, interaction: discord.Interaction) -> None:
        values = self.modal_values(interaction)
        password = values.get("password", "")
        current_password = values.get("current_password", "")
        token = self.sessions.token(interaction.user.id)
        if not token:
            await self._reply_key(interaction, "not_logged_in")
            return
        await self._defer(interaction)
        result = await self.http.settings(token, password=password, current_password=current_password)
        if not await self.consume_result(interaction, result):
            return
        await self._reply_key(interaction, "settings_pass_success")
