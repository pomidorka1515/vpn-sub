from __future__ import annotations

import discord
from bots.discord.composition import AdminFeatureMixin

__all__ = ["AdminCodesMixin"]


class AdminCodesMixin(AdminFeatureMixin):
    async def _cb_list_codes(self, interaction: discord.Interaction) -> None:
        await self._defer(interaction)
        raw = await self.sub.code_svc.list_code()
        if not raw:
            await self._respond(interaction, "Список кодов пуст.", view=self.codes_menu_view())
            return
        text = "🎟 **Список кодов:**\n\n" + "\n".join(f"- `{c}`" for c in raw)
        await self._respond(interaction, text, view=self.codes_menu_view())

    async def handle_info_code_modal(self, interaction: discord.Interaction) -> None:
        code = self.modal_values(interaction).get("code", "").strip()
        if not code:
            await self._respond(interaction, "❌ Введите код.", view=self.codes_menu_view())
            return
        await self._defer(interaction)
        typed = await self.sub.code_svc.get_code(code)
        text = (
            f"ℹ️ **Код: `{code}`**\n\n"
            f"Тип: `{typed.action}`\n"
            f"Перманентный: **{'Да' if typed.perma else 'Нет'}**\n"
            f"Использований: `{typed.uses}`\n"
            f"Дней: `{typed.days}`\n"
            f"Гигабайт: `{typed.gb}`\n"
            f"ВЛ Гигабайт: `{typed.wl_gb}`\n"
        )
        await self._respond(interaction, text, view=self.codes_menu_view())

    async def handle_del_code_modal(self, interaction: discord.Interaction) -> None:
        code = self.modal_values(interaction).get("code", "").strip()
        if not code:
            await self._respond(interaction, "❌ Введите код.", view=self.codes_menu_view())
            return
        await self._defer(interaction)
        await self.sub.code_svc.delete_code(code)
        await self._respond(interaction, f"✅ Код `{code}` удалён.", view=self.codes_menu_view())

    async def handle_add_code_name_modal(self, interaction: discord.Interaction) -> None:
        code_name = self.modal_values(interaction).get("code", "").strip()
        if not code_name:
            await self._respond(
                interaction,
                "❌ Введите название кода.",
                view=self.codes_menu_view(),
            )
            return
        self._pending_codes[interaction.user.id] = {"name": code_name}
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="📝 register",
                custom_id="admin:codetype:register",
                style=discord.ButtonStyle.secondary,
            ),
        )
        view.add_item(
            discord.ui.Button(
                label="🎁 bonus",
                custom_id="admin:codetype:bonus",
                style=discord.ButtonStyle.secondary,
            ),
        )
        view.add_item(
            discord.ui.Button(
                label="🔙 Отмена",
                custom_id="admin:codes",
                style=discord.ButtonStyle.primary,
            ),
        )
        await self._respond(interaction, f"Код: **{code_name}**\nВыберите тип:", view=view)

    async def handle_add_code_days_modal(self, interaction: discord.Interaction) -> None:
        pending = self._pending_codes.get(interaction.user.id)
        if not pending:
            await self._respond(
                interaction,
                "❌ Сессия истекла, начните заново.",
                view=self.codes_menu_view(),
            )
            return
        values = self.modal_values(interaction)
        try:
            days = int(values.get("days", "").strip())
            gb = int(values.get("gb", "").strip())
            wl_gb = int(values.get("wl_gb", "").strip())
        except ValueError:
            await self._respond(interaction, "❌ Введите число.", view=self.codes_menu_view())
            return
        pending["days"] = days
        pending["gb"] = gb
        pending["wl_gb"] = wl_gb
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="Да",
                custom_id="admin:code_perma:yes",
                style=discord.ButtonStyle.success,
            ),
        )
        view.add_item(
            discord.ui.Button(
                label="Нет",
                custom_id="admin:code_perma:no",
                style=discord.ButtonStyle.secondary,
            ),
        )
        view.add_item(
            discord.ui.Button(
                label="🔙 Отмена",
                custom_id="admin:codes",
                style=discord.ButtonStyle.primary,
            ),
        )
        await self._respond(interaction, "Перманентный код?", view=view)

    async def handle_add_code_uses_modal(self, interaction: discord.Interaction) -> None:
        pending = self._pending_codes.get(interaction.user.id)
        if not pending:
            await self._respond(
                interaction,
                "❌ Сессия истекла, начните заново.",
                view=self.codes_menu_view(),
            )
            return
        try:
            uses = int(self.modal_values(interaction).get("uses", "").strip())
            if uses < 1:
                raise ValueError
        except ValueError:
            await self._respond(
                interaction,
                "❌ Ошибка: кол-во должно быть числом больше 0.",
                view=self.codes_menu_view(),
            )
            return
        pending["uses"] = uses
        await self._finish_add_code(interaction)

    async def _finish_add_code(self, interaction: discord.Interaction) -> None:
        pending = self._pending_codes.pop(interaction.user.id, None)
        if not pending:
            await self._respond(
                interaction,
                "❌ Сессия истекла, начните заново.",
                view=self.codes_menu_view(),
            )
            return
        code_name = str(pending.get("name") or "")
        code_type = str(pending.get("type") or "")
        perma = bool(pending.get("perma"))
        days = int(pending.get("days") or 0)
        gb = int(pending.get("gb") or 0)
        wl_gb = int(pending.get("wl_gb") or 0)
        raw_uses = pending.get("uses")
        uses = int(raw_uses) if isinstance(raw_uses, int) else 1
        await self._defer(interaction)
        await self.sub.code_svc.add_code(
            code_name,
            code_type,
            permanent=perma,
            days=days,
            gb=gb,
            wl_gb=wl_gb,
            uses=uses,
        )
        await self._respond(
            interaction,
            (
                "✅ Код создан!\n\n"
                f"Код: `{code_name}`\n"
                f"Тип: `{code_type}`\n"
                f"Перманентный: **{'Да' if perma else 'Нет'}**\n"
                f"Дней: `{days}`\n"
                f"Кол-во использований: `{uses}`\n"
                f"Гб: `{gb}`\n"
                f"ВЛ Гб: `{wl_gb}`"
            ),
            view=self.codes_menu_view(),
        )
