from __future__ import annotations

import time
from datetime import UTC, datetime

import discord
from discord_bot.composition import AdminFeatureMixin

__all__ = ["AdminUsersMixin"]


class AdminUsersMixin(AdminFeatureMixin):
    async def _cb_list_users(self, interaction: discord.Interaction, page: int = 0) -> None:
        await self._defer(interaction)
        users = await self.sub.user_svc.list_users()
        if not users:
            await self._respond(
                interaction,
                "Список пользователей пуст.",
                view=self.main_menu_view(),
            )
            return
        total = len(users)
        total_pages = max(1, (total - 1) // self.USERS_PER_PAGE + 1)
        page = max(0, min(page, total_pages - 1))
        start = page * self.USERS_PER_PAGE
        page_users = users[start : start + self.USERS_PER_PAGE]
        text = f"👥 **Список пользователей** ({total} всего)\n\n"
        text += "\n".join([f"- `{u}`" for u in page_users])
        view = discord.ui.View(timeout=None)
        if page > 0:
            view.add_item(
                discord.ui.Button(
                    label="◀️",
                    custom_id=f"admin:page:list:{page - 1}",
                    style=discord.ButtonStyle.secondary,
                ),
            )
        if page < total_pages - 1:
            view.add_item(
                discord.ui.Button(
                    label="▶️",
                    custom_id=f"admin:page:list:{page + 1}",
                    style=discord.ButtonStyle.secondary,
                ),
            )
        if total_pages > 1:
            view.add_item(
                discord.ui.Button(
                    label=f"📄 {page + 1}/{total_pages}",
                    custom_id="admin:noop",
                    style=discord.ButtonStyle.secondary,
                    disabled=True,
                ),
            )
        view.add_item(
            discord.ui.Button(
                label="🔙 В меню",
                custom_id="admin:cancel",
                style=discord.ButtonStyle.primary,
            ),
        )
        await self._respond(interaction, text, view=view)

    async def _cb_online_users(self, interaction: discord.Interaction) -> None:
        await self._defer(interaction)
        result = await self.sub.panel_svc.get_online_status(True)
        users = result.users if isinstance(result.users, dict) else dict.fromkeys(result.users)
        if not users:
            await self._respond(
                interaction,
                "Нет пользователей в сети.",
                view=self.main_menu_view(),
            )
            return
        lines = [f"- `{u}`{', логин: ' + v if v else ''}" for u, v in users.items()]
        await self._respond(
            interaction,
            "👥 **Список пользователей онлайн:**\n\n" + "\n".join(lines),
            view=self.main_menu_view(),
        )

    async def _cb_refresh(self, interaction: discord.Interaction) -> None:
        await self._defer(interaction)
        result = await self.sub.refresh_svc.refresh()
        aborted = result.aborted
        if aborted:
            panel_failures = len(result.failed)
            succeeded = result.succeeded
            total = result.total
            await self._respond(
                interaction,
                (
                    "❌ Обновление прервано. "
                    f"Успешно: {succeeded}; панель недоступна: {panel_failures}; всего: {total}."
                ),
                view=self.main_menu_view(),
            )
            return
        if result.failed:
            names = ", ".join(result.failed)
            await self._respond(
                interaction,
                f"⚠️ Обновление завершено с ошибками: {names}",
                view=self.main_menu_view(),
            )
            return
        await self._respond(
            interaction,
            "✅ Все пользователи успешно обновлены.",
            view=self.main_menu_view(),
        )

    async def _cb_info_user(self, interaction: discord.Interaction, username: str) -> None:
        await self._defer(interaction)
        typed = await self.sub.business_svc.get_info(username, pretty=True)
        bandwidth = typed.bandwidth
        total = bandwidth.total
        wl_total = bandwidth.wl_total
        times = typed.time
        if times:
            days_left = str((times - int(time.time())) // 86400)
            date = datetime.fromtimestamp(times, tz=UTC).strftime("%d.%m.%y %H:%M (UTC)")
        else:
            days_left = "N/A"
            date = "N/A"
        status = "🟢 Включен" if typed.enabled else "🔴 Отключен"
        wl_status = "🟢 Включен" if typed.wl_enabled else "🔴 Отключен"
        online = "🟢 Да" if typed.online else "🔴 Нет"
        link = typed.link
        text = (
            f"ℹ️ **Информация о `{username}`**\n\n"
            f"Имя: `{typed.displayname}`\n"
            f"Статус: {status}\n"
            f"Статус WL: {wl_status}\n"
            f"В сети: {online}\n"
            f"Трафик в этом месяце: {bandwidth.monthly} MB / {bandwidth.limit} GB\n"
            f"Трафик WL в этом месяце: {bandwidth.wl_monthly} MB / {bandwidth.wl_limit} GB\n"
            f"Дата окончания: {date}\n"
            f"Дней осталось: {days_left}\n"
            f"Upload: {total.upload} MB | Download: {total.download} MB\n"
            f"WL Upload: {wl_total.upload} MB | Download: {wl_total.download} MB\n"
            f"Отпечаток: `{typed.fingerprint}`\n"
            f"Ссылка: `{link}`\n"
        )
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="✏️ Изменить пользователя",
                custom_id=f"admin:edit_user:{username}",
                style=discord.ButtonStyle.secondary,
            ),
        )
        view.add_item(
            discord.ui.Button(
                label="🔙 В меню",
                custom_id="admin:cancel",
                style=discord.ButtonStyle.primary,
            ),
        )
        await self._respond(interaction, text, view=view)

    async def _cb_del_user(self, interaction: discord.Interaction, username: str) -> None:
        await self._defer(interaction)
        await self.sub.business_svc.delete_user(username, perma=True)
        await self._respond(
            interaction,
            f"✅ Пользователь **{username}** удален.",
            view=self.main_menu_view(),
        )

    async def _cb_edit_user_options(self, interaction: discord.Interaction, username: str) -> None:
        self._pending_edits[interaction.user.id] = {"username": username}
        # Later edit buttons read this map. Write it before the menu is shown
        # and again after the response so a failed send cannot drop the user.
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="🔐 Отпечаток",
                custom_id="admin:edit:fp",
                style=discord.ButtonStyle.secondary,
            ),
        )
        view.add_item(
            discord.ui.Button(
                label="📊 Месячный лимит",
                custom_id="admin:edit:limit",
                style=discord.ButtonStyle.secondary,
            ),
        )
        view.add_item(
            discord.ui.Button(
                label="🌍 Мес. лимит WL",
                custom_id="admin:edit:wl_limit",
                style=discord.ButtonStyle.secondary,
            ),
        )
        view.add_item(
            discord.ui.Button(
                label="⏰ Срок",
                custom_id="admin:edit:time",
                style=discord.ButtonStyle.secondary,
            ),
        )
        view.add_item(
            discord.ui.Button(
                label="🏷 Отображаемое имя",
                custom_id="admin:edit:name",
                style=discord.ButtonStyle.secondary,
            ),
        )
        view.add_item(
            discord.ui.Button(
                label="🔙 Отмена",
                custom_id="admin:cancel",
                style=discord.ButtonStyle.primary,
            ),
        )
        self._pending_edits[interaction.user.id] = {"username": username}
        await self._respond(interaction, f"✏️ Что изменить для **{username}**?", view=view)

    async def _cb_edit_fingerprint(self, interaction: discord.Interaction, username: str) -> None:
        await self._defer(interaction)
        info = await self.sub.business_svc.get_info(username, pretty=False)
        current = info.fingerprint
        raw_fps = self.cfg.view()["fingerprints"]
        view = discord.ui.View(timeout=None)
        for fp in raw_fps:
            name = str(fp)
            label = f"✅ {name}" if name == current else name
            view.add_item(
                discord.ui.Button(
                    label=label,
                    custom_id=f"admin:fp_save:{name}",
                    style=discord.ButtonStyle.secondary,
                ),
            )
        view.add_item(
            discord.ui.Button(
                label="🔙 Отмена",
                custom_id="admin:cancel",
                style=discord.ButtonStyle.primary,
            ),
        )
        await self._respond(
            interaction,
            f"🔐 Выберите отпечаток для **{username}** (текущий: `{current}`):",
            view=view,
        )

    async def _cb_edit_limit(self, interaction: discord.Interaction, username: str) -> None:
        del username
        # Discord only accepts a modal as the first response. Fetching the
        # current limit first often exceeds the 3 second interaction window.
        modal = discord.ui.Modal(title="Месячный лимит", custom_id="admin:modal:edit_limit")
        modal.add_item(
            discord.ui.TextInput(
                label="Лимит GB (0 = безлимит)",
                custom_id="limit",
                min_length=1,
                max_length=8,
            ),
        )
        await self._send_modal(interaction, modal)

    async def _cb_edit_wl_limit(self, interaction: discord.Interaction, username: str) -> None:
        del username
        modal = discord.ui.Modal(title="WL лимит", custom_id="admin:modal:edit_wl_limit")
        modal.add_item(
            discord.ui.TextInput(
                label="Лимит GB (0 = безлимит)",
                custom_id="wl_limit",
                min_length=1,
                max_length=8,
            ),
        )
        await self._send_modal(interaction, modal)

    async def _cb_edit_time(self, interaction: discord.Interaction, username: str) -> None:
        del username
        modal = discord.ui.Modal(title="Срок", custom_id="admin:modal:edit_time")
        modal.add_item(
            discord.ui.TextInput(
                label="Дней (0 = безлимит)",
                custom_id="days",
                min_length=1,
                max_length=8,
            ),
        )
        await self._send_modal(interaction, modal)

    async def _cb_edit_name(self, interaction: discord.Interaction, username: str) -> None:
        del username
        modal = discord.ui.Modal(title="Отображаемое имя", custom_id="admin:modal:edit_name")
        modal.add_item(
            discord.ui.TextInput(
                label="Имя (макс. 16)",
                custom_id="name",
                min_length=1,
                max_length=16,
            ),
        )
        await self._send_modal(interaction, modal)

    async def handle_reset_user_modal(self, interaction: discord.Interaction) -> None:
        username = self.modal_values(interaction).get("username", "").strip()
        if not username:
            await self._respond(interaction, "❌ Введите username.", view=self.main_menu_view())
            return
        await self._defer(interaction)
        obj = await self.sub.business_svc.reset_user(username)
        await self._respond(
            interaction,
            f"✅ Пользователь был сброшен.\n\nToken: `{obj.token}`\nUUID: `{obj.uuid}`",
            view=self.main_menu_view(),
        )

    async def handle_add_user_modal(self, interaction: discord.Interaction) -> None:
        values = self.modal_values(interaction)
        username = values.get("username", "").strip()
        displayname = values.get("displayname", "").strip()
        try:
            limit = int(values.get("limit", "0").strip())
            days = int(values.get("days", "0").strip())
            if min(limit, days) < 0:
                raise ValueError
        except ValueError:
            await self._respond(
                interaction,
                "❌ Лимит и дни должны быть числами.",
                view=self.main_menu_view(),
            )
            return
        timee = int(time.time() + (days * 86400)) if days else 0
        await self._defer(interaction)
        await self.sub.business_svc.add_new_user(
            username=username, displayname=displayname, limit=limit, timee=timee
        )
        await self._respond(
            interaction,
            f"✅ Пользователь **{username}** успешно добавлен!",
            view=self.main_menu_view(),
        )

    async def handle_edit_limit_modal(self, interaction: discord.Interaction) -> None:
        pending = self._pending_edits.get(interaction.user.id)
        if not pending:
            await self._respond(
                interaction,
                "❌ Сессия истекла, начните с /admin",
                view=self.main_menu_view(),
            )
            return
        try:
            limit = int(self.modal_values(interaction).get("limit", "").strip())
            if limit < 0:
                raise ValueError
        except ValueError:
            await self._respond(interaction, "❌ Введите число.", view=self.main_menu_view())
            return
        await self.sub.business_svc.update_params(username=pending["username"], limit=limit)
        self._pending_edits.pop(interaction.user.id, None)
        await self._respond(
            interaction,
            f"✅ Месячный лимит обновлён: `{limit}` GB",
            view=self.main_menu_view(),
        )

    async def handle_edit_wl_limit_modal(self, interaction: discord.Interaction) -> None:
        pending = self._pending_edits.get(interaction.user.id)
        if not pending:
            await self._respond(
                interaction,
                "❌ Сессия истекла, начните с /admin",
                view=self.main_menu_view(),
            )
            return
        try:
            wl_limit = int(self.modal_values(interaction).get("wl_limit", "").strip())
            if wl_limit < 0:
                raise ValueError
        except ValueError:
            await self._respond(interaction, "❌ Введите число.", view=self.main_menu_view())
            return
        await self.sub.business_svc.update_params(username=pending["username"], wl_limit=wl_limit)
        self._pending_edits.pop(interaction.user.id, None)
        await self._respond(
            interaction,
            f"✅ Лимит обновлён: `{wl_limit}` GB",
            view=self.main_menu_view(),
        )

    async def handle_edit_time_modal(self, interaction: discord.Interaction) -> None:
        pending = self._pending_edits.get(interaction.user.id)
        if not pending:
            await self._respond(
                interaction,
                "❌ Сессия истекла, начните с /admin",
                view=self.main_menu_view(),
            )
            return
        try:
            days = int(self.modal_values(interaction).get("days", "").strip())
            if days < 0:
                raise ValueError
        except ValueError:
            await self._respond(interaction, "❌ Введите число.", view=self.main_menu_view())
            return
        timee = int(time.time() + (days * 86400)) if days else 0
        await self.sub.business_svc.update_params(username=pending["username"], timee=timee)
        self._pending_edits.pop(interaction.user.id, None)
        if days:
            new_date = datetime.fromtimestamp(timee, tz=UTC).strftime("%d.%m.%y %H:%M (UTC)")
            await self._respond(
                interaction,
                f"✅ Срок продлён на `{days}` дней, новая дата: `{new_date}`",
                view=self.main_menu_view(),
            )
            return
        await self._respond(
            interaction,
            "✅ Срок установлен в безлимит.",
            view=self.main_menu_view(),
        )

    async def handle_edit_name_modal(self, interaction: discord.Interaction) -> None:
        pending = self._pending_edits.get(interaction.user.id)
        if not pending:
            await self._respond(
                interaction,
                "❌ Сессия истекла, начните с /admin",
                view=self.main_menu_view(),
            )
            return
        new_name = self.modal_values(interaction).get("name", "").strip()
        if len(new_name) > 16:
            await self._respond(
                interaction,
                "❌ Имя слишком длинное (макс. 16 символов).",
                view=self.main_menu_view(),
            )
            return
        await self.sub.business_svc.update_params(
            username=pending["username"], displayname=new_name
        )
        self._pending_edits.pop(interaction.user.id, None)
        await self._respond(
            interaction,
            f"✅ Имя обновлено: `{new_name}`",
            view=self.main_menu_view(),
        )
