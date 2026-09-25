from __future__ import annotations

from typing import Mapping, cast

import discord

from composition import PublicFeatureMixin

__all__ = ["PublicRoutingMixin"]

_AUTH_PREFIXES = (
    "set_",
    "fp_",
    "chart_",
    "confirm_",
)
_AUTH_IDS = (
    "menu_account",
    "menu_sub",
    "menu_info",
    "menu_get_sub",
    "menu_bonus",
    "menu_reset",
    "menu_chart",
    "menu_settings",
    "menu_logout",
    "menu_help",
    "menu_delete",
    "fp_select",
    "chart_select",
)
_AUTH_MODALS = (
    "bonus_modal",
    "delete_modal",
    "settings_name_modal",
    "settings_login_modal",
    "settings_pass_modal",
)
_AUTH_COMMANDS = (
    "info",
    "sub",
    "bonus",
    "chart",
    "settings",
    "help",
    "logout",
    "reset",
    "delete",
)


class PublicRoutingMixin(PublicFeatureMixin):
    def _custom_id(self, interaction: discord.Interaction) -> str:
        data = cast(Mapping[str, object] | None, interaction.data)
        if not data:
            return ""
        custom_id = data.get("custom_id")
        return custom_id if isinstance(custom_id, str) else ""

    def _select_value(self, interaction: discord.Interaction) -> str | None:
        data = cast(Mapping[str, object] | None, interaction.data)
        if not data:
            return None
        values_raw = data.get("values")
        if isinstance(values_raw, list) and values_raw:
            values = cast(list[str], values_raw)
            return str(values[0])
        return None

    def _requires_auth(self, custom_id: str) -> bool:
        if custom_id in ("menu_main", "confirm_cancel"):
            return False
        if custom_id in _AUTH_IDS or custom_id in _AUTH_MODALS:
            return True
        return custom_id.startswith(_AUTH_PREFIXES)

    async def cmd_help(self, interaction: discord.Interaction) -> None:
        token = self.sessions.token(interaction.user.id)
        if not token:
            await self._reply_key(interaction, "not_logged_in")
            return
        await self._defer(interaction)
        lang = self.get_lang(interaction.user.id)
        result = await self.http.profiles(token, lang)
        if not await self.consume_result(interaction, result):
            return
        raw_obj = result.obj
        if not isinstance(raw_obj, dict):
            await self._reply_key(interaction, "bad_response")
            return
        obj = cast(dict[str, str], raw_obj)
        lines: list[str] = []
        for name, desc in obj.items():
            lines.append(f"`{name}` — {desc}")
        text = self.text(lang, "help_text", text="\n".join(lines))
        if text:
            await self._respond(interaction, text)
        else:
            await self._reply_key(interaction, "bad_response")

    async def dispatch_component(self, interaction: discord.Interaction) -> None:
        custom_id = self._custom_id(interaction)
        if not custom_id:
            return
        uid = interaction.user.id
        if self._requires_auth(custom_id) and not self.is_logged_in(uid):
            await self._reply_key(interaction, "not_logged_in")
            return
        if custom_id.startswith("lang_"):
            await self.set_lang_callback(interaction, custom_id.split("_", 1)[1])
            return
        if custom_id == "login_credentials":
            await self.cmd_login(interaction)
            return
        if custom_id == "login_register":
            await self.cmd_register(interaction)
            return
        if custom_id == "menu_lang":
            await self._respond(
                interaction,
                self.text(self.get_lang(uid), "choose_lang"),
                view=self.language_view(),
            )
            return
        if custom_id == "menu_support":
            await self.cmd_support(interaction)
            return
        if custom_id == "menu_account":
            await self._reply_key(interaction, "welcome_reg", view=self.account_menu_view(self.get_lang(uid)))
            return
        if custom_id == "menu_sub":
            await self._reply_key(interaction, "welcome_reg", view=self.subscription_menu_view(self.get_lang(uid)))
            return
        if custom_id == "menu_main":
            await self.cmd_start(interaction)
            return
        if custom_id == "menu_info":
            await self.cmd_info(interaction)
            return
        if custom_id == "menu_get_sub":
            await self.cmd_sub(interaction)
            return
        if custom_id == "menu_bonus":
            await self.cmd_bonus(interaction)
            return
        if custom_id == "menu_reset":
            await self.cmd_reset(interaction)
            return
        if custom_id == "menu_chart":
            await self.cmd_chart(interaction)
            return
        if custom_id == "menu_settings":
            await self.cmd_settings(interaction)
            return
        if custom_id == "menu_logout":
            await self.cmd_logout(interaction)
            return
        if custom_id == "menu_help":
            await self.cmd_help(interaction)
            return
        if custom_id == "menu_delete":
            await self.cmd_delete(interaction)
            return
        if custom_id == "set_name":
            await self.open_name_modal(interaction)
            return
        if custom_id == "set_fp":
            await self.open_fingerprint_menu(interaction)
            return
        if custom_id == "set_login":
            await self.open_login_modal(interaction)
            return
        if custom_id == "set_pass":
            await self.open_pass_modal(interaction)
            return
        if custom_id == "fp_select":
            value = self._select_value(interaction) or ""
            if value:
                await self.apply_fingerprint(interaction, value)
            return
        if custom_id == "chart_select":
            raw = self._select_value(interaction) or ""
            try:
                days = int(raw)
            except ValueError:
                return
            await self.render_chart(interaction, days)
            return
        if custom_id == "confirm_logout":
            await self.confirm_logout(interaction)
            return
        if custom_id == "confirm_reset":
            await self.confirm_reset(interaction)
            return
        if custom_id == "confirm_cancel":
            await self._reply_key(interaction, "cancelled")
            return

    async def dispatch_modal(self, interaction: discord.Interaction) -> None:
        custom_id = self._custom_id(interaction)
        if self._requires_auth(custom_id) and not self.is_logged_in(interaction.user.id):
            await self._reply_key(interaction, "not_logged_in")
            return
        if custom_id == "login_modal":
            await self.handle_login_modal(interaction)
            return
        if custom_id == "register_modal":
            await self.handle_register_modal(interaction)
            return
        if custom_id == "bonus_modal":
            await self.handle_bonus_modal(interaction)
            return
        if custom_id == "delete_modal":
            await self.handle_delete_modal(interaction)
            return
        if custom_id == "settings_name_modal":
            await self.handle_name_modal(interaction)
            return
        if custom_id == "settings_login_modal":
            await self.handle_login_change_modal(interaction)
            return
        if custom_id == "settings_pass_modal":
            await self.handle_pass_change_modal(interaction)
            return

    def command_requires_auth(self, name: str) -> bool:
        return name in _AUTH_COMMANDS
