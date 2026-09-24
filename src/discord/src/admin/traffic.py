from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, cast

import discord

from chart import bandwidth_chart
from composition import AdminFeatureMixin
from custom_types import BandwidthSnapshot
from util import fmt_bytes, format_usage, truncate_utf8
from .common import obj_map, result_obj

__all__ = ["AdminTrafficMixin"]


class AdminTrafficMixin(AdminFeatureMixin):
    def _chart_lang(self) -> Mapping[str, str]:
        raw = self.lang_cfg.get("chart")
        if isinstance(raw, dict):
            ru = raw.get("ru")
            if isinstance(ru, dict):
                return {str(k): str(v) for k, v in ru.items()}
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
            await self._respond(interaction, "❌ Введите число от 1 до 90.", view=self.main_menu_view())
            return
        await self._defer(interaction)
        await self._respond(interaction, "⏳ Генерация графика...")
        history = await self.http.history(username, days)
        if not await self.consume_result(interaction, history):
            return
        info = await self.http.user_info(username, pretty=False)
        if not await self.consume_result(interaction, info):
            return
        payload: object = result_obj(info)
        if not isinstance(payload, dict):
            await self._respond(interaction, "❌ Некорректный ответ сервиса.", view=self.main_menu_view())
            return
        obj = obj_map(cast(object, payload))
        bandwidth = obj_map(obj.get("bandwidth"))
        total = obj_map(bandwidth.get("total"))
        wl_total = obj_map(bandwidth.get("wl_total"))
        used_str, limit_str, percent_str = format_usage(bandwidth.get("monthly") or 0, bandwidth.get("limit") or 0)
        wl_used_str, wl_limit_str, wl_percent_str = format_usage(bandwidth.get("wl_monthly") or 0, bandwidth.get("wl_limit") or 0)
        text = f"""📈 **График трафика за {days} дней**

**Пользователь:** `{username}` ({obj.get('displayname')})

**Общий трафик:**
├ Upload: {fmt_bytes(total.get('upload') or 0)}
├ Download: {fmt_bytes(total.get('download') or 0)}
├ Использовано: {used_str} / {limit_str}
└ Процент: {percent_str}

**WL трафик:**
├ Upload: {fmt_bytes(wl_total.get('upload') or 0)}
├ Download: {fmt_bytes(wl_total.get('download') or 0)}
├ Использовано: {wl_used_str} / {wl_limit_str}
└ Процент: {wl_percent_str}"""
        text = truncate_utf8(text, 2000)
        snapshots: list[BandwidthSnapshot] = []
        history_raw = history.obj
        items = cast(list[dict[str, Any]], history_raw) if isinstance(history_raw, list) else []
        for item in items:
            snapshots.append(
                BandwidthSnapshot(
                    ts=int(item.get("ts") or 0),
                    up=int(item.get("up") or 0),
                    down=int(item.get("down") or 0),
                    wl_up=int(item.get("wl_up") or 0),
                    wl_down=int(item.get("wl_down") or 0),
                )
            )
        image = await asyncio.to_thread(
            bandwidth_chart,
            snapshots,
            label=str(obj.get("displayname") or ""),
            lang=self._chart_lang(),
        )
        if image is not None:
            image.seek(0)
            file = discord.File(image, filename="chart.png")
            await self._respond(interaction, text, file=file, view=self.main_menu_view())
            return
        await self._respond(interaction, text + "\n\n❌ Нет данных для графика", view=self.main_menu_view())
