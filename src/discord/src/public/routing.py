from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import cast

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
    async def _show_lang_menu(self, interaction: discord.Interaction) -> None:
        await self._respond(
            interaction,
            self.text(self.get_lang(interaction.user.id), "choose_lang"),
            view=self.language_view(),
        )

    async def _show_account_menu(self, interaction: discord.Interaction) -> None:
        await self._reply_key(
            interaction,
            "welcome_reg",
            view=self.account_menu_view(self.get_lang(interaction.user.id)),
        )

    async def _show_sub_menu(self, interaction: discord.Interaction) -> None:
        await self._reply_key(
            interaction,
            "welcome_reg",
            view=self.subscription_menu_view(self.get_lang(interaction.user.id)),
        )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await self._reply_key(interaction, "cancelled")

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
        obj = raw_obj
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
        if self._requires_auth(custom_id) and not self.is_logged_in(interaction.user.id):
            await self._reply_key(interaction, "not_logged_in")
            return
        if custom_id.startswith("lang_"):
            await self.set_lang_callback(interaction, custom_id.split("_", 1)[1])
            return
        handler_name = _EXACT_HANDLERS.get(custom_id)
        if handler_name is None:
            return
        handler = cast(
            Callable[[discord.Interaction], Awaitable[None]], getattr(self, handler_name)
        )
        await handler(interaction)

    async def _dispatch_fp_select(self, interaction: discord.Interaction) -> None:
        value = self._select_value(interaction) or ""
        if value:
            await self.apply_fingerprint(interaction, value)

    async def _dispatch_chart_select(self, interaction: discord.Interaction) -> None:
        raw = self._select_value(interaction) or ""
        try:
            days = int(raw)
        except ValueError:
            return
        await self.render_chart(interaction, days)

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


_EXACT_HANDLERS: dict[str, str] = {
    "login_credentials": "cmd_login",
    "login_register": "cmd_register",
    "menu_lang": "_show_lang_menu",
    "menu_support": "cmd_support",
    "menu_account": "_show_account_menu",
    "menu_sub": "_show_sub_menu",
    "menu_main": "cmd_start",
    "menu_info": "cmd_info",
    "menu_get_sub": "cmd_sub",
    "menu_bonus": "cmd_bonus",
    "menu_reset": "cmd_reset",
    "menu_chart": "cmd_chart",
    "menu_settings": "cmd_settings",
    "menu_logout": "cmd_logout",
    "menu_help": "cmd_help",
    "menu_delete": "cmd_delete",
    "set_name": "open_name_modal",
    "set_fp": "open_fingerprint_menu",
    "set_login": "open_login_modal",
    "set_pass": "open_pass_modal",
    "fp_select": "_dispatch_fp_select",
    "chart_select": "_dispatch_chart_select",
    "confirm_logout": "confirm_logout",
    "confirm_reset": "confirm_reset",
    "confirm_cancel": "_cancel",
}
