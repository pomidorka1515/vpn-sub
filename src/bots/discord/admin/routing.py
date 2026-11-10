from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, Mapping
from typing import ClassVar, cast

import discord

from bots.discord.composition import AdminFeatureMixin

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
        ("admin:code_cancel:", "_handle_code_cancel", True),
        ("admin:code_retry:", "_handle_code_retry", True),
        ("admin:chart", "_handle_chart", False),
        ("admin:leaderboard", "_handle_leaderboard", False),
        ("admin:edit_user:", "_handle_edit_user", True),
        ("admin:edit:", "_handle_edit", True),
        ("admin:fp_save:", "_handle_fingerprint_save", True),
        ("admin:lbt:", "_handle_leaderboard_type", True),
        ("admin:lbo:", "_handle_leaderboard_order", True),
    )

    MODALS: ClassVar[dict[str, str]] = {
        "admin:modal:add_user": "handle_add_user_modal",
        "admin:modal:reset_user": "handle_reset_user_modal",
        "admin:modal:edit_limit": "handle_edit_limit_modal",
        "admin:modal:edit_wl_limit": "handle_edit_wl_limit_modal",
        "admin:modal:edit_time": "handle_edit_time_modal",
        "admin:modal:edit_name": "handle_edit_name_modal",
        "admin:modal:edit_login": "handle_edit_login_modal",
        "admin:modal:edit_date": "handle_edit_date_modal",
        "admin:modal:info_code": "handle_info_code_modal",
        "admin:modal:del_code": "handle_del_code_modal",
        "admin:modal:add_code": "handle_add_code_name_modal",
        "admin:modal:code_days": "handle_add_code_days_modal",
        "admin:modal:code_uses": "handle_add_code_uses_modal",
        "admin:modal:chart": "handle_chart_modal",
        "admin:modal:lb_window": "handle_leaderboard_window_modal",
    }

    def _custom_id(self, interaction: discord.Interaction) -> str:
        data = cast(Mapping[str, object] | None, interaction.data)
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
            self.log.exception("admin component dispatch failed")
            await self._respond(interaction, "⚠️ Внутренняя ошибка", view=self.main_menu_view())

    async def dispatch_modal(self, interaction: discord.Interaction) -> None:
        if not await self.require_admin(interaction):
            return
        custom_id = self._custom_id(interaction)
        route = (
            custom_id.rsplit(":", 1)[0]
            if custom_id.startswith(
                (
                    "admin:modal:edit_",
                    "admin:modal:add_code:",
                    "admin:modal:code_days:",
                    "admin:modal:code_uses:",
                )
            )
            else custom_id
        )
        handler_name = self.MODALS.get(route)
        if handler_name is None:
            return
        try:
            handler = cast(
                Callable[[discord.Interaction], Awaitable[None]],
                getattr(self, handler_name),
            )
            await handler(interaction)
        except Exception:
            self.log.exception("admin modal dispatch failed")
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
        modal.add_item(
            discord.ui.TextInput(
                label="Username",
                custom_id="username",
                min_length=1,
                max_length=64,
            ),
        )
        await self._send_modal(interaction, modal)

    async def _handle_add_user(self, data: str, interaction: discord.Interaction) -> None:
        del data
        modal = discord.ui.Modal(title="Добавить пользователя", custom_id="admin:modal:add_user")
        modal.add_item(
            discord.ui.TextInput(
                label="Username",
                custom_id="username",
                min_length=1,
                max_length=64,
            ),
        )
        modal.add_item(
            discord.ui.TextInput(
                label="Отображаемое имя",
                custom_id="displayname",
                min_length=1,
                max_length=16,
            ),
        )
        modal.add_item(
            discord.ui.TextInput(
                label="Лимит GB (0 = безлимит)",
                custom_id="limit",
                min_length=1,
                max_length=8,
            ),
        )
        modal.add_item(
            discord.ui.TextInput(
                label="Дней (0 = безлимит)",
                custom_id="days",
                min_length=1,
                max_length=8,
            ),
        )
        await self._send_modal(interaction, modal)

    async def _load_users(self, interaction: discord.Interaction) -> list[str] | None:
        del interaction
        return await self.sub.user_svc.list_users()

    async def _handle_info_menu(self, data: str, interaction: discord.Interaction) -> None:
        del data
        users = await self._load_users(interaction)
        if users is None:
            return
        if not users:
            await self._respond(
                interaction,
                "Список пользователей пуст.",
                view=self.main_menu_view(),
            )
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
        modal.add_item(
            discord.ui.TextInput(label="Код", custom_id="code", min_length=1, max_length=64),
        )
        await self._send_modal(interaction, modal)

    async def _handle_delete_code(self, data: str, interaction: discord.Interaction) -> None:
        del data
        modal = discord.ui.Modal(title="Удалить код", custom_id="admin:modal:del_code")
        modal.add_item(
            discord.ui.TextInput(label="Код", custom_id="code", min_length=1, max_length=64),
        )
        await self._send_modal(interaction, modal)

    async def _handle_add_code(self, data: str, interaction: discord.Interaction) -> None:
        del data
        generation = secrets.token_urlsafe(24)
        pending: dict[str, str | int | bool] = {"generation": generation, "stage": "name"}
        self._pending_codes[interaction.user.id] = pending
        try:
            await self._send_code_modal(interaction, generation, "name")
        except BaseException:
            if self._pending_codes.get(interaction.user.id) is pending:
                self._pending_codes.pop(interaction.user.id, None)
            raise

    async def _handle_panel_status(self, data: str, interaction: discord.Interaction) -> None:
        del data
        await self._cb_all_panels_status(interaction)

    async def _handle_code_type(self, data: str, interaction: discord.Interaction) -> None:
        code_type, _, generation = data.removeprefix("admin:codetype:").partition(":")
        pending = await self._code_pending(interaction, generation, "type")
        if pending is None:
            return
        if code_type not in {"register", "bonus"}:
            await self._respond(
                interaction, "❌ Неизвестный тип кода.", view=self.codes_menu_view()
            )
            return
        pending.update(type=code_type, stage="days")
        try:
            await self._send_code_modal(interaction, generation, "days")
        except BaseException:
            if (
                self._pending_codes.get(interaction.user.id) is pending
                and pending.get("stage") == "days"
            ):
                pending["stage"] = "type"
            raise

    async def _handle_code_perma(self, data: str, interaction: discord.Interaction) -> None:
        selection, _, generation = data.removeprefix("admin:code_perma:").partition(":")
        pending = await self._code_pending(interaction, generation, "perma")
        if pending is None:
            return
        if selection not in {"yes", "no"}:
            await self._respond(
                interaction, "❌ Неизвестный выбор.", view=self._code_perma_view(generation)
            )
            return
        perma = selection == "yes"
        pending["perma"] = perma
        if perma:
            pending["uses"] = -1
            await self._finish_add_code(interaction)
            return
        pending["stage"] = "uses"
        try:
            await self._send_code_modal(interaction, generation, "uses")
        except BaseException:
            if (
                self._pending_codes.get(interaction.user.id) is pending
                and pending.get("stage") == "uses"
            ):
                pending["stage"] = "perma"
            raise

    async def _handle_code_cancel(self, data: str, interaction: discord.Interaction) -> None:
        stage, _, generation = data.removeprefix("admin:code_cancel:").partition(":")
        if stage not in {"name", "type", "days", "perma", "uses"}:
            await self._respond(
                interaction, "❌ Сессия истекла, начните заново.", view=self.codes_menu_view()
            )
            return
        pending = await self._code_pending(interaction, generation, stage)
        if pending is None:
            return
        self._pending_codes.pop(interaction.user.id, None)
        await self._respond(interaction, "Действие отменено.", view=self.codes_menu_view())

    async def _handle_code_retry(self, data: str, interaction: discord.Interaction) -> None:
        stage, _, generation = data.removeprefix("admin:code_retry:").partition(":")
        if stage not in {"name", "days", "uses"}:
            await self._respond(
                interaction, "❌ Сессия истекла, начните заново.", view=self.codes_menu_view()
            )
            return
        pending = await self._code_pending(interaction, generation, stage)
        if pending is None:
            return
        await self._send_code_modal(interaction, generation, stage)

    async def _handle_chart(self, data: str, interaction: discord.Interaction) -> None:
        del data
        modal = discord.ui.Modal(title="История трафика", custom_id="admin:modal:chart")
        modal.add_item(
            discord.ui.TextInput(
                label="Username",
                custom_id="username",
                min_length=1,
                max_length=64,
            ),
        )
        modal.add_item(
            discord.ui.TextInput(label="Дней (1-90)", custom_id="days", min_length=1, max_length=3),
        )
        await self._send_modal(interaction, modal)

    async def _handle_leaderboard(self, data: str, interaction: discord.Interaction) -> None:
        del data
        await self._cb_leaderboard_type(interaction)

    async def _handle_edit_user(self, data: str, interaction: discord.Interaction) -> None:
        await self._cb_edit_user_options(interaction, data.split("admin:edit_user:", 1)[1])

    async def _handle_edit(self, data: str, interaction: discord.Interaction) -> None:
        action, _, generation = data.removeprefix("admin:edit:").partition(":")
        pending = self._pending_edits.get(interaction.user.id)
        if not pending or not generation or pending.get("generation") != generation:
            await self._respond(
                interaction,
                "❌ Сессия истекла, начните с /admin",
                view=self.main_menu_view(),
            )
            return
        handlers = {
            "fp": self._cb_edit_fingerprint,
            "limit": self._cb_edit_limit,
            "wl_limit": self._cb_edit_wl_limit,
            "time": self._cb_edit_time,
            "name": self._cb_edit_name,
            "login": self._cb_edit_login,
            "date": self._cb_edit_date,
            "password": self._cb_password_reset,
        }
        handler = handlers.get(action)
        if handler is not None:
            await handler(interaction, pending["username"])

    async def _handle_fingerprint_save(self, data: str, interaction: discord.Interaction) -> None:
        fp, _, generation = data.removeprefix("admin:fp_save:").partition(":")
        pending = self._pending_edits.get(interaction.user.id)
        if not pending or not generation or pending.get("generation") != generation:
            await self._respond(
                interaction,
                "❌ Сессия истекла, начните с /admin",
                view=self.main_menu_view(),
            )
            return
        if fp not in self.cfg.view()["fingerprints"]:
            await self._respond(
                interaction, "❌ Неизвестный отпечаток.", view=self.main_menu_view()
            )
            return
        if not await self._update_edit_params(interaction, pending, fingerprint=fp):
            return
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
            await self._respond(
                interaction,
                "❌ Сессия истекла, начните заново.",
                view=self.main_menu_view(),
            )
            return
        pending["order"] = data.split("admin:lbo:", 1)[1]
        modal = discord.ui.Modal(title="Таблица лидеров", custom_id="admin:modal:lb_window")
        modal.add_item(
            discord.ui.TextInput(
                label="Количество записей (0 = все)",
                custom_id="window",
                min_length=1,
                max_length=8,
            ),
        )
        await self._send_modal(interaction, modal)
