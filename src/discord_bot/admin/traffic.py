from __future__ import annotations

from typing import TYPE_CHECKING, cast

import discord
from chart import bandwidth_chart
from discord_bot.composition import AdminFeatureMixin
from util import fmt_bytes, format_usage, truncate_utf8

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["AdminTrafficMixin"]


class AdminTrafficMixin(AdminFeatureMixin):
    def _chart_lang(self) -> Mapping[str, str]:
        language = self.lang_cfg.view()
        raw = cast(object, language.get("chart"))
        if isinstance(raw, dict):
            ru = cast(dict[object, object], raw).get("ru")
            if isinstance(ru, dict):
                return {str(k): str(v) for k, v in cast(dict[object, object], ru).items()}
        return {
            "bandwidth": "Использование трафика",
            "days": "дн.",
            "day": "день",
            "regular_traffic": "Обычный трафик",
            "whitelist_traffic": "Белый список",
            "download": "Загрузка",
            "upload": "Отдача",
            "no_data": "Нет данных",
        }

    async def handle_chart_modal(self, interaction: discord.Interaction) -> None:
        values = self.modal_values(interaction)
        username = values.get("username", "").strip()
        try:
            days = int(values.get("days", "").strip())
            if not 1 <= days <= 90:
                raise ValueError
        except ValueError:
            await self._respond(
                interaction,
                "❌ Введите число от 1 до 90.",
                view=self.main_menu_view(),
            )
            return
        await self._defer(interaction)
        await self._respond(interaction, "⏳ Генерация графика...")
        snapshots = await self.sub.bandwidth_svc.get_bw_history(username, days)
        obj = await self.sub.business_svc.get_info(username, pretty=False)
        bandwidth = obj.bandwidth
        total = bandwidth.total
        wl_total = bandwidth.wl_total
        used_str, limit_str, percent_str = format_usage(
            bandwidth.monthly,
            bandwidth.limit,
        )
        wl_used_str, wl_limit_str, wl_percent_str = format_usage(
            bandwidth.wl_monthly,
            bandwidth.wl_limit,
        )
        text = f"""📈 **График трафика за {days} дней**

**Пользователь:** `{username}` ({obj.displayname})

**Общий трафик:**
├ Upload: {fmt_bytes(total.upload)}
├ Download: {fmt_bytes(total.download)}
├ Использовано: {used_str} / {limit_str}
└ Процент: {percent_str}

**WL трафик:**
├ Upload: {fmt_bytes(wl_total.upload)}
├ Download: {fmt_bytes(wl_total.download)}
├ Использовано: {wl_used_str} / {wl_limit_str}
└ Процент: {wl_percent_str}"""
        text = truncate_utf8(text, 2000)
        image = await self.rendering.run(
            bandwidth_chart,
            snapshots,
            label=obj.displayname,
            lang=self._chart_lang(),
        )
        if image is not None:
            image.seek(0)
            file = discord.File(image, filename="chart.png")
            await self._respond(interaction, text, file=file, view=self.main_menu_view())
            return
        await self._respond(
            interaction,
            text + "\n\n❌ Нет данных для графика",
            view=self.main_menu_view(),
        )
