from __future__ import annotations

from collections.abc import Callable, Awaitable
from typing import Any, Mapping, cast

import discord

from composition import AdminFeatureMixin
from .common import result_obj, str_list

__all__ = ["AdminRoutingMixin"]


class AdminRoutingMixin(AdminFeatureMixin):
    ROUTES: tuple[tuple[str, str, bool], ...] = (
        ("admin:cancel", "_handle_cancel", False),
        ("admin:noop", "_handle_noop", False),
        ("admin:menu", "_handle_cancel", False),
        ("admin:online_users", "_handle_online_users", False),
        ("admin:list_users", "_handle_list_users", False),
        ("admin:page:list:", "_handle_list_users_page", True),
        ("admin:refresh_all", "_handle_refresh", False),
        ("admin:reset_user", "_handle_reset_user", False),
        ("admin:add_user", "_handle_add_user", False),
        ("admin:info", "_handle_info_menu", False),
        ("admin:page:info:", "_handle_info_page", True),
        ("admin:info_code", "_handle_info_code", False),
        ("admin:info:", "_handle_info_user", True),
        ("admin:action_del", "_handle_delete_menu", False),
        ("admin:page:dodel:", "_handle_delete_page", True),
        ("admin:dodel:", "_handle_delete_user", True),
        ("admin:codes", "_handle_codes_menu", False),
        ("admin:list_codes", "_handle_list_codes", False),
        ("admin:del_code", "_handle_delete_code", False),
        ("admin:add_code", "_handle_add_code", False),
        ("admin:status_panels", "_handle_panel_status", False),
        ("admin:codetype:", "_handle_code_type", True),
        ("admin:code_perma:", "_handle_code_perma", True),
        ("admin:chart", "_handle_chart", False),
        ("admin:leaderboard", "_handle_leaderboard", False),
        ("admin:edit_user:", "_handle_edit_user", True),
        ("admin:edit:", "_handle_edit", True),
        ("admin:fp_save:", "_handle_fingerprint_save", True),
        ("admin:lbt:", "_handle_leaderboard_type", True),
        ("admin:lbo:", "_handle_leaderboard_order", True),
    )

    MODALS: dict[str, str] = {
        "admin:modal:add_user": "handle_add_user_modal",
        "admin:modal:reset_user": "handle_reset_user_modal",
        "admin:modal:edit_limit": "handle_edit_limit_modal",
        "admin:modal:edit_wl_limit": "handle_edit_wl_limit_modal",
        "admin:modal:edit_time": "handle_edit_time_modal",
        "admin:modal:edit_name": "handle_edit_name_modal",
        "admin:modal:info_code": "handle_info_code_modal",
        "admin:modal:del_code": "handle_del_code_modal",
        "admin:modal:add_code": "handle_add_code_name_modal",
        "admin:modal:code_days": "handle_add_code_days_modal",
        "admin:modal:code_uses": "handle_add_code_uses_modal",
        "admin:modal:chart": "handle_chart_modal",
        "admin:modal:lb_window": "handle_leaderboard_window_modal",
    }

    def _custom_id(self, interaction: discord.Interaction) -> str:
        data = cast(Mapping[str, Any] | None, interaction.data)
        if not data:
            return ""
        custom_id = data.get("custom_id")
        return custom_id if isinstance(custom_id, str) else ""

    async def dispatch_component(self, interaction: discord.Interaction) -> None:
        if not await self.require_admin(interaction):
            return
        data = self._custom_id(interaction)
        if not data:
            return
        try:
            for route, handler_name, is_prefix in self.ROUTES:
                if data.startswith(route) if is_prefix else data == route:
                    handler = cast(
                        Callable[[str, discord.Interaction], Awaitable[None]],
                        getattr(self, handler_name),
                    )
                    await handler(data, interaction)
                    return
        except Exception:
            self.log.error("admin component dispatch failed", exc_info=True)
            await self._respond(interaction, "⚠️ Внутренняя ошибка", view=self.main_menu_view())

    async def dispatch_modal(self, interaction: discord.Interaction) -> None:
        if not await self.require_admin(interaction):
            return
        custom_id = self._custom_id(interaction)
        handler_name = self.MODALS.get(custom_id)
        if handler_name is None:
            return
        try:
            handler = cast(Callable[[discord.Interaction], Awaitable[None]], getattr(self, handler_name))
            await handler(interaction)
        except Exception:
            self.log.error("admin modal dispatch failed", exc_info=True)
            await self._respond(interaction, "⚠️ Внутренняя ошибка", view=self.main_menu_view())

    async def _handle_cancel(self, data: str, interaction: discord.Interaction) -> None:
        del data
        uid = interaction.user.id
        self._pending_edits.pop(uid, None)
        self._pending_codes.pop(uid, None)
        self._pending_leaderboard.pop(uid, None)
        await self.show_main_menu(interaction, "Действие отменено. Главное меню:")

    async def _handle_noop(self, data: str, interaction: discord.Interaction) -> None:
        del data
        await self._defer(interaction)

    async def _handle_online_users(self, data: str, interaction: discord.Interaction) -> None:
        del data
        await self._cb_online_users(interaction)

    async def _handle_list_users(self, data: str, interaction: discord.Interaction) -> None:
        del data
        await self._cb_list_users(interaction)

    async def _handle_list_users_page(self, data: str, interaction: discord.Interaction) -> None:
        await self._cb_list_users(interaction, int(data.rsplit(":", 1)[1]))

    async def _handle_refresh(self, data: str, interaction: discord.Interaction) -> None:
        del data
        await self._cb_refresh(interaction)

    async def _handle_reset_user(self, data: str, interaction: discord.Interaction) -> None:
        del data
        modal = discord.ui.Modal(title="Сброс пользователя", custom_id="admin:modal:reset_user")
        modal.add_item(discord.ui.TextInput(label="Username", custom_id="username", min_length=1, max_length=64))
        await self._send_modal(interaction, modal)

    async def _handle_add_user(self, data: str, interaction: discord.Interaction) -> None:
        del data
        modal = discord.ui.Modal(title="Добавить пользователя", custom_id="admin:modal:add_user")
        modal.add_item(discord.ui.TextInput(label="Username", custom_id="username", min_length=1, max_length=64))
        modal.add_item(discord.ui.TextInput(label="Отображаемое имя", custom_id="displayname", min_length=1, max_length=16))
        modal.add_item(discord.ui.TextInput(label="Лимит GB (0 = безлимит)", custom_id="limit", min_length=1, max_length=8))
        modal.add_item(discord.ui.TextInput(label="Дней (0 = безлимит)", custom_id="days", min_length=1, max_length=8))
        await self._send_modal(interaction, modal)

    async def _load_users(self, interaction: discord.Interaction) -> list[str] | None:
        result = await self.http.list_users()
        if not await self.consume_result(interaction, result):
            return None
        raw: object = result_obj(result)
        if not isinstance(raw, list):
            await self._respond(interaction, "❌ Некорректный ответ сервиса.", view=self.main_menu_view())
            return None
        return str_list(cast(object, raw))

    async def _handle_info_menu(self, data: str, interaction: discord.Interaction) -> None:
        del data
        users = await self._load_users(interaction)
        if users is None:
            return
        if not users:
            await self._respond(interaction, "Список пользователей пуст.", view=self.main_menu_view())
            return
        uid = interaction.user.id
        page = self._pagination_state.get(uid, {}).get("info_page", 0)
        self._pagination_state.setdefault(uid, {})["info_page"] = page
        await self._respond(
            interaction,
            "👤 Выберите пользователя для просмотра информации:",
            view=self.users_menu_view("info", users, page),
        )

    async def _handle_info_page(self, data: str, interaction: discord.Interaction) -> None:
        page = int(data.rsplit(":", 1)[1])
        users = await self._load_users(interaction)
        if users is None:
            return
        uid = interaction.user.id
        self._pagination_state.setdefault(uid, {})["info_page"] = page
        await self._respond(
            interaction,
            "👤 Выберите пользователя для просмотра информации:",
            view=self.users_menu_view("info", users, page),
        )

    async def _handle_info_user(self, data: str, interaction: discord.Interaction) -> None:
        await self._cb_info_user(interaction, data.split("admin:info:", 1)[1])

    async def _handle_delete_menu(self, data: str, interaction: discord.Interaction) -> None:
        del data
        users = await self._load_users(interaction)
        if users is None:
            return
        if not users:
            await self._respond(interaction, "Список пуст.", view=self.main_menu_view())
            return
        uid = interaction.user.id
        page = self._pagination_state.get(uid, {}).get("del_page", 0)
        await self._respond(
            interaction,
            "Выберите пользователя для УДАЛЕНИЯ ⚠️:",
            view=self.users_menu_view("dodel", users, page),
        )

    async def _handle_delete_page(self, data: str, interaction: discord.Interaction) -> None:
        page = int(data.rsplit(":", 1)[1])
        users = await self._load_users(interaction)
        if users is None:
            return
        uid = interaction.user.id
        self._pagination_state.setdefault(uid, {})["del_page"] = page
        await self._respond(
            interaction,
            "Выберите пользователя для УДАЛЕНИЯ ⚠️:",
            view=self.users_menu_view("dodel", users, page),
        )

    async def _handle_delete_user(self, data: str, interaction: discord.Interaction) -> None:
        await self._cb_del_user(interaction, data.split("admin:dodel:", 1)[1])

    async def _handle_codes_menu(self, data: str, interaction: discord.Interaction) -> None:
        del data
        await self._respond(interaction, "🎟 Управление кодами:", view=self.codes_menu_view())

    async def _handle_list_codes(self, data: str, interaction: discord.Interaction) -> None:
        del data
        await self._cb_list_codes(interaction)

    async def _handle_info_code(self, data: str, interaction: discord.Interaction) -> None:
        del data
        modal = discord.ui.Modal(title="Инфо о коде", custom_id="admin:modal:info_code")
        modal.add_item(discord.ui.TextInput(label="Код", custom_id="code", min_length=1, max_length=64))
        await self._send_modal(interaction, modal)

    async def _handle_delete_code(self, data: str, interaction: discord.Interaction) -> None:
        del data
        modal = discord.ui.Modal(title="Удалить код", custom_id="admin:modal:del_code")
        modal.add_item(discord.ui.TextInput(label="Код", custom_id="code", min_length=1, max_length=64))
        await self._send_modal(interaction, modal)

    async def _handle_add_code(self, data: str, interaction: discord.Interaction) -> None:
        del data
        modal = discord.ui.Modal(title="Добавить код", custom_id="admin:modal:add_code")
        modal.add_item(discord.ui.TextInput(label="Название кода", custom_id="code", min_length=1, max_length=64))
        await self._send_modal(interaction, modal)

    async def _handle_panel_status(self, data: str, interaction: discord.Interaction) -> None:
        del data
        await self._cb_all_panels_status(interaction)

    async def _handle_code_type(self, data: str, interaction: discord.Interaction) -> None:
        pending = self._pending_codes.get(interaction.user.id)
        if not pending:
            await self._respond(interaction, "❌ Сессия истекла, начните заново.", view=self.codes_menu_view())
            return
        code_type = data.split("admin:codetype:", 1)[1]
        pending["type"] = code_type
        self._pending_codes[interaction.user.id] = pending
        modal = discord.ui.Modal(title="Параметры кода", custom_id="admin:modal:code_days")
        modal.add_item(discord.ui.TextInput(label="Дней", custom_id="days", min_length=1, max_length=8))
        modal.add_item(discord.ui.TextInput(label="Гигабайты", custom_id="gb", min_length=1, max_length=8))
        modal.add_item(discord.ui.TextInput(label="ВЛ гигабайты", custom_id="wl_gb", min_length=1, max_length=8))
        await self._send_modal(interaction, modal)

    async def _handle_code_perma(self, data: str, interaction: discord.Interaction) -> None:
        pending = self._pending_codes.get(interaction.user.id)
        if not pending:
            await self._respond(interaction, "❌ Сессия истекла, начните заново.", view=self.codes_menu_view())
            return
        perma = data.endswith(":yes")
        pending["perma"] = perma
        if perma:
            pending["uses"] = -1
            await self._finish_add_code(interaction)
            return
        modal = discord.ui.Modal(title="Использования кода", custom_id="admin:modal:code_uses")
        modal.add_item(discord.ui.TextInput(label="Кол-во использований (>= 1)", custom_id="uses", min_length=1, max_length=8))
        await self._send_modal(interaction, modal)

    async def _handle_chart(self, data: str, interaction: discord.Interaction) -> None:
        del data
        modal = discord.ui.Modal(title="История трафика", custom_id="admin:modal:chart")
        modal.add_item(discord.ui.TextInput(label="Username", custom_id="username", min_length=1, max_length=64))
        modal.add_item(discord.ui.TextInput(label="Дней (1-90)", custom_id="days", min_length=1, max_length=3))
        await self._send_modal(interaction, modal)

    async def _handle_leaderboard(self, data: str, interaction: discord.Interaction) -> None:
        del data
        await self._cb_leaderboard_type(interaction)

    async def _handle_edit_user(self, data: str, interaction: discord.Interaction) -> None:
        await self._cb_edit_user_options(interaction, data.split("admin:edit_user:", 1)[1])

    async def _handle_edit(self, data: str, interaction: discord.Interaction) -> None:
        pending = self._pending_edits.get(interaction.user.id)
        if not pending:
            await self._respond(interaction, "❌ Сессия истекла, начните с /admin", view=self.main_menu_view())
            return
        handlers = {
            "fp": self._cb_edit_fingerprint,
            "limit": self._cb_edit_limit,
            "wl_limit": self._cb_edit_wl_limit,
            "time": self._cb_edit_time,
            "name": self._cb_edit_name,
        }
        handler = handlers.get(data.split("admin:edit:", 1)[1])
        if handler is not None:
            await handler(interaction, pending["username"])

    async def _handle_fingerprint_save(self, data: str, interaction: discord.Interaction) -> None:
        pending = self._pending_edits.get(interaction.user.id)
        if not pending:
            await self._respond(interaction, "❌ Сессия истекла, начните с /admin", view=self.main_menu_view())
            return
        fp = data.split("admin:fp_save:", 1)[1]
        result = await self.http.update_user(pending["username"], fingerprint=fp)
        if not await self.consume_result(interaction, result):
            return
        self._pending_edits.pop(interaction.user.id, None)
        await self._respond(
            interaction,
            f"✅ fingerprint обновлён: `{fp}`",
            view=self.main_menu_view(),
        )

    async def _handle_leaderboard_type(self, data: str, interaction: discord.Interaction) -> None:
        self._pending_leaderboard[interaction.user.id] = {"type": data.split("admin:lbt:", 1)[1]}
        await self._cb_leaderboard_order(interaction)

    async def _handle_leaderboard_order(self, data: str, interaction: discord.Interaction) -> None:
        pending = self._pending_leaderboard.get(interaction.user.id)
        if pending is None:
            await self._respond(interaction, "❌ Сессия истекла, начните заново.", view=self.main_menu_view())
            return
        pending["order"] = data.split("admin:lbo:", 1)[1]
        modal = discord.ui.Modal(title="Таблица лидеров", custom_id="admin:modal:lb_window")
        modal.add_item(discord.ui.TextInput(label="Количество записей (0 = все)", custom_id="window", min_length=1, max_length=8))
        await self._send_modal(interaction, modal)
