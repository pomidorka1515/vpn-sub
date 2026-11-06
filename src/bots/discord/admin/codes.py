from __future__ import annotations

import discord
from bots.discord.composition import AdminFeatureMixin
from bots.list_messages import list_messages
from errors import CommittedAuditError

__all__ = ["AdminCodesMixin"]


class AdminCodesMixin(AdminFeatureMixin):
    async def _cb_list_codes(self, interaction: discord.Interaction) -> None:
        await self._defer(interaction)
        raw = await self.sub.code_svc.list_code()
        if not raw:
            await self._respond(interaction, "Список кодов пуст.", view=self.codes_menu_view())
            return
        for text in list_messages(
            "🎟 **Список кодов:**\n\n", ((code, None) for code in raw), limit=2000
        ):
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

    async def _code_pending(
        self, interaction: discord.Interaction, generation: str, stage: str
    ) -> dict[str, str | int | bool] | None:
        pending = self._pending_codes.get(interaction.user.id)
        if (
            not generation
            or not pending
            or pending.get("generation") != generation
            or pending.get("stage") != stage
        ):
            await self._respond(
                interaction, "❌ Сессия истекла, начните заново.", view=self.codes_menu_view()
            )
            return None
        return pending

    def _code_retry_view(self, generation: str, stage: str) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(label="Повторить", custom_id=f"admin:code_retry:{stage}:{generation}")
        )
        view.add_item(
            discord.ui.Button(
                label="🔙 Отмена",
                custom_id=f"admin:code_cancel:{stage}:{generation}",
                style=discord.ButtonStyle.primary,
            )
        )
        return view

    async def _send_code_modal(
        self, interaction: discord.Interaction, generation: str, stage: str
    ) -> None:
        if stage == "name":
            modal = discord.ui.Modal(
                title="Добавить код", custom_id=f"admin:modal:add_code:{generation}"
            )
            modal.add_item(
                discord.ui.TextInput(
                    label="Название кода",
                    custom_id=f"code:{generation}",
                    min_length=1,
                    max_length=64,
                )
            )
        elif stage == "days":
            modal = discord.ui.Modal(
                title="Параметры кода", custom_id=f"admin:modal:code_days:{generation}"
            )
            modal.add_item(
                discord.ui.TextInput(
                    label="Дней", custom_id=f"days:{generation}", min_length=1, max_length=8
                )
            )
            modal.add_item(
                discord.ui.TextInput(
                    label="Гигабайты", custom_id=f"gb:{generation}", min_length=1, max_length=8
                )
            )
            modal.add_item(
                discord.ui.TextInput(
                    label="ВЛ гигабайты",
                    custom_id=f"wl_gb:{generation}",
                    min_length=1,
                    max_length=8,
                )
            )
        else:
            modal = discord.ui.Modal(
                title="Использования кода", custom_id=f"admin:modal:code_uses:{generation}"
            )
            modal.add_item(
                discord.ui.TextInput(
                    label="Кол-во использований (>= 1)",
                    custom_id=f"uses:{generation}",
                    min_length=1,
                    max_length=8,
                )
            )
        await self._send_modal(interaction, modal)

    def _code_perma_view(self, generation: str) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="Да",
                custom_id=f"admin:code_perma:yes:{generation}",
                style=discord.ButtonStyle.success,
            )
        )
        view.add_item(
            discord.ui.Button(
                label="Нет",
                custom_id=f"admin:code_perma:no:{generation}",
                style=discord.ButtonStyle.secondary,
            )
        )
        view.add_item(
            discord.ui.Button(
                label="🔙 Отмена",
                custom_id=f"admin:code_cancel:perma:{generation}",
                style=discord.ButtonStyle.primary,
            )
        )
        return view

    async def handle_add_code_name_modal(self, interaction: discord.Interaction) -> None:
        generation = self._custom_id(interaction).removeprefix("admin:modal:add_code:")
        pending = await self._code_pending(interaction, generation, "name")
        if pending is None:
            return
        code_name = self.modal_values(interaction).get(f"code:{generation}", "").strip()
        if not code_name:
            await self._respond(
                interaction,
                "❌ Введите название кода.",
                view=self._code_retry_view(generation, "name"),
            )
            return
        pending.update(name=code_name, stage="type")
        view = discord.ui.View(timeout=None)
        for code_type, label in (("register", "📝 register"), ("bonus", "🎁 bonus")):
            view.add_item(
                discord.ui.Button(
                    label=label,
                    custom_id=f"admin:codetype:{code_type}:{generation}",
                    style=discord.ButtonStyle.secondary,
                )
            )
        view.add_item(
            discord.ui.Button(
                label="🔙 Отмена",
                custom_id=f"admin:code_cancel:type:{generation}",
                style=discord.ButtonStyle.primary,
            )
        )
        await self._respond(interaction, f"Код: **{code_name}**\nВыберите тип:", view=view)

    async def handle_add_code_days_modal(self, interaction: discord.Interaction) -> None:
        generation = self._custom_id(interaction).removeprefix("admin:modal:code_days:")
        pending = await self._code_pending(interaction, generation, "days")
        if pending is None:
            return
        values = self.modal_values(interaction)
        try:
            days = int(values.get(f"days:{generation}", "").strip())
            gb = int(values.get(f"gb:{generation}", "").strip())
            wl_gb = int(values.get(f"wl_gb:{generation}", "").strip())
            if min(days, gb, wl_gb) < 0:
                raise ValueError
        except ValueError:
            await self._respond(
                interaction, "❌ Введите число.", view=self._code_retry_view(generation, "days")
            )
            return
        pending.update(days=days, gb=gb, wl_gb=wl_gb, stage="perma")
        await self._respond(
            interaction, "Перманентный код?", view=self._code_perma_view(generation)
        )

    async def handle_add_code_uses_modal(self, interaction: discord.Interaction) -> None:
        generation = self._custom_id(interaction).removeprefix("admin:modal:code_uses:")
        pending = await self._code_pending(interaction, generation, "uses")
        if pending is None:
            return
        try:
            uses = int(self.modal_values(interaction).get(f"uses:{generation}", "").strip())
            if uses < 1:
                raise ValueError
        except ValueError:
            await self._respond(
                interaction,
                "❌ Ошибка: кол-во должно быть числом больше 0.",
                view=self._code_retry_view(generation, "uses"),
            )
            return
        pending["uses"] = uses
        await self._finish_add_code(interaction)

    async def _finish_add_code(self, interaction: discord.Interaction) -> None:
        # Claim synchronously before the first await; other interactions cannot commit it twice.
        pending = self._pending_codes[interaction.user.id]
        previous_stage = str(pending["stage"])
        pending["stage"] = "busy"
        generation = str(pending["generation"])
        code_name = str(pending["name"])
        code_type = str(pending["type"])
        perma = bool(pending["perma"])
        days = int(pending["days"])
        gb = int(pending["gb"])
        wl_gb = int(pending["wl_gb"])
        uses = int(pending["uses"])
        try:
            await self._defer(interaction)
            await self.sub.code_svc.add_code(
                code_name, code_type, permanent=perma, days=days, gb=gb, wl_gb=wl_gb, uses=uses
            )
        except CommittedAuditError:
            self.log.exception("code created but audit failed")
            if self._pending_codes.get(interaction.user.id) is pending:
                self._pending_codes.pop(interaction.user.id, None)
            await self._respond(
                interaction,
                f"⚠️ Код `{code_name}` создан, но запись аудита не удалась. Не повторяйте создание.",
                view=self.codes_menu_view(),
            )
            return
        except Exception:
            self.log.exception("code creation failed")
            if self._pending_codes.get(interaction.user.id) is pending:
                pending["stage"] = previous_stage
                view = (
                    self._code_perma_view(generation)
                    if previous_stage == "perma"
                    else self._code_retry_view(generation, previous_stage)
                )
            else:
                view = self.codes_menu_view()
            await self._respond(
                interaction, "⚠️ Не удалось создать код. Повторите попытку.", view=view
            )
            return
        except BaseException:
            if self._pending_codes.get(interaction.user.id) is pending:
                pending["stage"] = previous_stage
            raise
        if self._pending_codes.get(interaction.user.id) is pending:
            self._pending_codes.pop(interaction.user.id, None)
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
