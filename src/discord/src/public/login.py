from __future__ import annotations

from typing import Any

import discord

from composition import PublicFeatureMixin

__all__ = ["PublicLoginMixin"]


class LoginModal(discord.ui.Modal):
    def __init__(self, title: str, username_label: str, password_label: str) -> None:
        super().__init__(title=title, custom_id="login_modal")
        self.username: discord.ui.TextInput[Any] = discord.ui.TextInput(
            label=username_label,
            custom_id="username",
            min_length=1,
            max_length=32,
        )
        self.password: discord.ui.TextInput[Any] = discord.ui.TextInput(
            label=password_label,
            custom_id="password",
            min_length=1,
            max_length=128,
            style=discord.TextStyle.short,
        )
        self.add_item(self.username)
        self.add_item(self.password)


class RegisterModal(discord.ui.Modal):
    def __init__(
        self,
        title: str,
        username_label: str,
        password_label: str,
        code_label: str,
        name_label: str,
    ) -> None:
        super().__init__(title=title, custom_id="register_modal")
        self.username: discord.ui.TextInput[Any] = discord.ui.TextInput(
            label=username_label,
            custom_id="username",
            min_length=1,
            max_length=32,
        )
        self.password: discord.ui.TextInput[Any] = discord.ui.TextInput(
            label=password_label,
            custom_id="password",
            min_length=1,
            max_length=128,
        )
        self.code: discord.ui.TextInput[Any] = discord.ui.TextInput(
            label=code_label,
            custom_id="code",
            min_length=1,
            max_length=64,
        )
        self.name: discord.ui.TextInput[Any] = discord.ui.TextInput(
            label=name_label,
            custom_id="name",
            min_length=1,
            max_length=16,
        )
        self.add_item(self.username)
        self.add_item(self.password)
        self.add_item(self.code)
        self.add_item(self.name)


class PublicLoginMixin(PublicFeatureMixin):
    async def cmd_login(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        if self.is_logged_in(uid):
            await self._reply_key(interaction, "already_logged_in")
            return
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        modal = LoginModal(
            title=t.get("btn_login", "Login"),
            username_label=t.get("enter_email", "Username"),
            password_label=t.get("enter_pass", "Password"),
        )
        try:
            await interaction.response.send_modal(modal)
        except Exception:
            self.log.error("failed to open login modal", exc_info=True)

    async def cmd_register(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        if self.is_logged_in(uid):
            await self._reply_key(interaction, "already_logged_in")
            return
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        modal = RegisterModal(
            title=t.get("btn_register", "Register"),
            username_label=t.get("enter_email", "Username"),
            password_label=t.get("enter_pass", "Password"),
            code_label=t.get("enter_invite", "Invite code"),
            name_label=t.get("name_label", "Name"),
        )
        try:
            await interaction.response.send_modal(modal)
        except Exception:
            self.log.error("failed to open register modal", exc_info=True)

    async def handle_login_modal(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        if self.is_logged_in(uid):
            await self._reply_key(interaction, "already_logged_in")
            return
        values = self.modal_values(interaction)
        username = values.get("username", "").strip()
        password = values.get("password", "")
        await self._defer(interaction)
        result = await self.http.login(username, password)
        if result.status == 401:
            await self._reply_key(interaction, "login_fail")
            return
        if not await self.consume_result(interaction, result):
            return
        token = result.auth_token
        if not token:
            await self._reply_key(interaction, "http_unavailable")
            return
        self.sessions.set_token(uid, token)
        lang = self.get_lang(uid)
        await self._reply_key(
            interaction,
            "login_success",
            view=self.main_menu_view(lang, logged_in=True),
        )
        await self.send_info(interaction)

    async def handle_register_modal(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        if self.is_logged_in(uid):
            await self._reply_key(interaction, "already_logged_in")
            return
        values = self.modal_values(interaction)
        username = values.get("username", "").strip()
        password = values.get("password", "")
        code = values.get("code", "").strip()
        name = values.get("name", "").strip()
        await self._defer(interaction)
        check = await self.http.validate_username(username)
        if not await self.consume_result(interaction, check):
            return
        obj = check.obj
        if not isinstance(obj, dict):
            await self._reply_key(interaction, "bad_response")
            return
        if not obj.get("valid"):
            await self._reply_key(interaction, "length_username", ln=32)
            return
        if obj.get("taken"):
            await self._reply_key(interaction, "username_taken")
            return
        result = await self.http.register(username, password, code, name)
        if not result.ok:
            if result.status in (400, 404, 409) and result.msg:
                await self._respond(interaction, result.msg)
                return
            if not await self.consume_result(interaction, result):
                return
            return
        token = result.auth_token
        if not token:
            await self._reply_key(interaction, "http_unavailable")
            return
        self.sessions.set_token(uid, token)
        lang = self.get_lang(uid)
        await self._reply_key(
            interaction,
            "reg_success",
            view=self.main_menu_view(lang, logged_in=True),
        )
        await self.send_info(interaction)
