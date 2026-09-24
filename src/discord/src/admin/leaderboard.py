from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Literal, cast

import discord

from chart import leaderboard_chart
from composition import AdminFeatureMixin
from .common import result_obj
from util import fmt_bytes, truncate_utf8

__all__ = ["AdminLeaderboardMixin"]


class AdminLeaderboardMixin(AdminFeatureMixin):
    def _chart_lang(self) -> Mapping[str, str]:
        raw = self.lang_cfg.get("chart")
        if isinstance(raw, dict):
            ru = raw.get("ru")
            if isinstance(ru, dict):
                return {str(k): str(v) for k, v in ru.items()}
        return {
            "leaderboard": "Таблица лидеров",
            "bw_type_total": "весь трафик",
            "bw_type_monthly": "трафик за месяц",
            "bw_type_wl_monthly": "WL-трафик за месяц",
        }

    async def _cb_leaderboard_type(self, interaction: discord.Interaction) -> None:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="📊 Общий (total)", custom_id="admin:lbt:total", style=discord.ButtonStyle.secondary))
        view.add_item(discord.ui.Button(label="📅 Месячный (monthly)", custom_id="admin:lbt:monthly", style=discord.ButtonStyle.secondary))
        view.add_item(discord.ui.Button(label="🛡 Белый список (wl_monthly)", custom_id="admin:lbt:wl_monthly", style=discord.ButtonStyle.secondary))
        await self._respond(interaction, "🏆 **Таблица лидеров**\n\nВыберите тип трафика:", view=view)

    async def _cb_leaderboard_order(self, interaction: discord.Interaction) -> None:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="🔽 По убыванию (1st = most)", custom_id="admin:lbo:desc", style=discord.ButtonStyle.secondary))
        view.add_item(discord.ui.Button(label="🔼 По возрастанию (1st = least)", custom_id="admin:lbo:asc", style=discord.ButtonStyle.secondary))
        await self._respond(interaction, "📋 **Сортировка**\n\nВыберите порядок:", view=view)

    async def handle_leaderboard_window_modal(self, interaction: discord.Interaction) -> None:
        pending = self._pending_leaderboard.pop(interaction.user.id, None)
        if not pending:
            await self._respond(interaction, "❌ Сессия истекла, начните заново.", view=self.main_menu_view())
            return
        try:
            window = int(self.modal_values(interaction).get("window", "").strip())
            if window < 0:
                raise ValueError
        except ValueError:
            await self._respond(interaction, "❌ Введите неотрицательное целое число.", view=self.main_menu_view())
            return
        bw_type = cast(Literal["total", "monthly", "wl_monthly"], pending.get("type", "total"))
        order = cast(Literal["asc", "desc"], pending.get("order", "desc"))
        await self._defer(interaction)
        result = await self.http.leaderboard(
            bw_type,
            window,
            displaynames=True,
            flip=order == "asc",
        )
        if not await self.consume_result(interaction, result):
            return
        raw = result_obj(result)
        rows = cast(list[dict[str, Any]], raw) if isinstance(raw, list) else []
        lb_data: dict[str, int] = {}
        for item in rows:
            user = str(item.get("username") or "")
            amount = int(item.get("amount") or 0)
            if user:
                lb_data[user] = amount
        if not lb_data:
            await self._respond(interaction, "Нет данных!", view=self.main_menu_view())
            return
        image = await asyncio.to_thread(
            leaderboard_chart,
            lb_data,
            bandwidth_type=bw_type,
            lang=self._chart_lang(),
        )
        sorted_items = sorted(lb_data.items(), key=lambda x: x[1], reverse=(order == "desc"))
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines: list[str] = []
        for rank, (user, score) in enumerate(sorted_items, start=1):
            prefix = medals.get(rank, f"{rank}.")
            lines.append(f"{prefix} **{user}** - {fmt_bytes(score)}")
        text = truncate_utf8("**Список лидеров по трафику:**\n\n" + "\n".join(lines), 2000)
        file: discord.File | None = None
        if image is not None:
            image.seek(0)
            file = discord.File(image, filename="leaderboard.png")
        await self._respond(interaction, text, file=file, view=self.main_menu_view())
