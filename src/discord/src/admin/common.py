from __future__ import annotations

from typing import Any, Mapping, cast

import discord

from composition import AdminFeatureMixin
from webapi import ApiResult

__all__ = ["AdminCommonMixin", "obj_map", "result_obj", "str_list"]


def obj_map(value: object) -> dict[str, Any]:
    raw: object = value
    if not isinstance(raw, Mapping):
        return {}
    mapping = cast(Mapping[object, object], raw)
    out: dict[str, Any] = {}
    for key, item in mapping.items():
        out[str(key)] = item
    return out


def str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in cast(list[object], value)]


def result_obj(result: ApiResult) -> object:
    payload: object = result.obj
    return payload


class AdminCommonMixin(AdminFeatureMixin):
    USERS_PER_PAGE = 10

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_uids

    def in_dm(self, interaction: discord.Interaction) -> bool:
        channel = interaction.channel
        if interaction.guild_id is None:
            return True
        return isinstance(channel, discord.DMChannel)

    def _ephemeral(self, interaction: discord.Interaction, ephemeral: bool | None) -> bool:
        if ephemeral is not None:
            return ephemeral
        return not self.in_dm(interaction)

    async def _respond(
        self,
        interaction: discord.Interaction,
        content: str | None,
        *,
        ephemeral: bool | None = None,
        view: discord.ui.View | None = None,
        file: discord.File | None = None,
    ) -> None:
        if not content and view is None and file is None:
            return
        ephemeral = self._ephemeral(interaction, ephemeral)
        kwargs: dict[str, Any] = {"ephemeral": ephemeral}
        if content is not None:
            kwargs["content"] = content
        if view is not None:
            kwargs["view"] = view
        if file is not None:
            kwargs["file"] = file
        try:
            if interaction.response.is_done():
                kwargs.pop("ephemeral", None)
                if ephemeral:
                    kwargs["ephemeral"] = True
                await interaction.followup.send(**kwargs)
            else:
                await interaction.response.send_message(**kwargs)
        except Exception:
            self.log.error("failed to respond to admin interaction", exc_info=True)

    async def _defer(self, interaction: discord.Interaction, *, ephemeral: bool | None = None) -> None:
        if interaction.response.is_done():
            return
        try:
            await interaction.response.defer(ephemeral=self._ephemeral(interaction, ephemeral))
        except Exception:
            self.log.error("failed to defer admin interaction", exc_info=True)

    async def _send_modal(self, interaction: discord.Interaction, modal: discord.ui.Modal) -> None:
        if interaction.response.is_done():
            return
        try:
            await interaction.response.send_modal(modal)
        except Exception:
            self.log.error("failed to send admin modal", exc_info=True)

    def modal_values(self, interaction: discord.Interaction) -> dict[str, str]:
        out: dict[str, str] = {}
        data = cast(Mapping[str, Any] | None, interaction.data)
        if not data:
            return out
        rows_raw = data.get("components")
        if not isinstance(rows_raw, list):
            return out
        rows = cast(list[dict[str, Any]], rows_raw)
        for row in rows:
            items_raw = row.get("components")
            if not isinstance(items_raw, list):
                continue
            items = cast(list[dict[str, Any]], items_raw)
            for item in items:
                cid = item.get("custom_id")
                if isinstance(cid, str):
                    value = item.get("value")
                    out[cid] = value if isinstance(value, str) else str(value or "")
        return out

    async def consume_result(
        self,
        interaction: discord.Interaction,
        result: ApiResult,
        *,
        view: discord.ui.View | None = None,
    ) -> bool:
        if result.status == 401:
            await self._respond(interaction, "❌ Нет доступа к API.", view=view or self.main_menu_view())
            return False
        if result.status == 429:
            await self._respond(interaction, "⏳ Слишком много запросов. Подождите минуту.", view=view or self.main_menu_view())
            return False
        if result.status == 0 or result.msg == "http_unavailable":
            await self._respond(interaction, "⚠ Сервис временно недоступен.", view=view or self.main_menu_view())
            return False
        if not result.ok:
            await self._respond(
                interaction,
                f"❌ {result.msg}" if result.msg else "❌ Внутренняя ошибка",
                view=view or self.main_menu_view(),
            )
            return False
        return True

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if self.is_admin(interaction.user.id):
            return True
        await self._respond(interaction, "❌ Нет доступа.", ephemeral=True)
        return False

    def main_menu_view(self) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        buttons = (
            ("👥 Список юзеров", "admin:list_users", discord.ButtonStyle.secondary),
            ("ℹ️ Инфо о юзере", "admin:info", discord.ButtonStyle.secondary),
            ("➕ Добавить", "admin:add_user", discord.ButtonStyle.success),
            ("❌ Удалить", "admin:action_del", discord.ButtonStyle.danger),
            ("🔄 Обновить всех", "admin:refresh_all", discord.ButtonStyle.secondary),
            ("🎟 Коды", "admin:codes", discord.ButtonStyle.secondary),
            ("🟢 Пользователи онлайн", "admin:online_users", discord.ButtonStyle.secondary),
            ("⚠ Сбросить пользователя", "admin:reset_user", discord.ButtonStyle.danger),
            ("📈 История трафика", "admin:chart", discord.ButtonStyle.secondary),
            ("🏆 Таблица лидеров", "admin:leaderboard", discord.ButtonStyle.secondary),
            ("ℹ️ Статус панелей", "admin:status_panels", discord.ButtonStyle.secondary),
        )
        for label, custom_id, style in buttons:
            view.add_item(discord.ui.Button(label=label, custom_id=custom_id, style=style))
        return view

    def codes_menu_view(self) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="➕ Добавить код", custom_id="admin:add_code", style=discord.ButtonStyle.success))
        view.add_item(discord.ui.Button(label="❌ Удалить код", custom_id="admin:del_code", style=discord.ButtonStyle.danger))
        view.add_item(discord.ui.Button(label="📋 Список кодов", custom_id="admin:list_codes", style=discord.ButtonStyle.secondary))
        view.add_item(discord.ui.Button(label="ℹ️ Инфо о коде", custom_id="admin:info_code", style=discord.ButtonStyle.secondary))
        view.add_item(discord.ui.Button(label="🔙 В меню", custom_id="admin:cancel", style=discord.ButtonStyle.primary))
        return view

    def cancel_view(self) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="🔙 Отмена / В меню", custom_id="admin:cancel", style=discord.ButtonStyle.primary))
        return view

    def users_menu_view(self, prefix: str, users: list[str], page: int = 0) -> discord.ui.View:
        total = len(users)
        total_pages = max(1, (total - 1) // self.USERS_PER_PAGE + 1)
        page = max(0, min(page, total_pages - 1))
        start = page * self.USERS_PER_PAGE
        page_users = users[start:start + self.USERS_PER_PAGE]
        view = discord.ui.View(timeout=None)
        for user in page_users:
            view.add_item(
                discord.ui.Button(
                    label=user,
                    custom_id=f"admin:{prefix}:{user}",
                    style=discord.ButtonStyle.secondary,
                )
            )
        if page > 0:
            view.add_item(discord.ui.Button(label="◀️", custom_id=f"admin:page:{prefix}:{page - 1}", style=discord.ButtonStyle.secondary))
        if page < total_pages - 1:
            view.add_item(discord.ui.Button(label="▶️", custom_id=f"admin:page:{prefix}:{page + 1}", style=discord.ButtonStyle.secondary))
        if total_pages > 1:
            view.add_item(discord.ui.Button(label=f"📄 {page + 1}/{total_pages}", custom_id="admin:noop", style=discord.ButtonStyle.secondary, disabled=True))
        view.add_item(discord.ui.Button(label="🔙 Отмена / В меню", custom_id="admin:cancel", style=discord.ButtonStyle.primary))
        return view

    async def show_main_menu(self, interaction: discord.Interaction, text: str | None = None) -> None:
        await self._respond(
            interaction,
            text or "👋 Привет! Панель управления VPN запущена.",
            view=self.main_menu_view(),
        )

    async def cmd_admin(self, interaction: discord.Interaction) -> None:
        if not await self.require_admin(interaction):
            return
        await self.show_main_menu(interaction)
