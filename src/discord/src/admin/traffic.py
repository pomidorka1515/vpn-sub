from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import cast

import discord
from chart import bandwidth_chart
from composition import AdminFeatureMixin
from custom_types import BandwidthSnapshot
from payloads import number, object_rows
from util import fmt_bytes, format_usage, truncate_utf8

from .common import obj_map, result_obj

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
        used_str, limit_str, percent_str = format_usage(number(bandwidth.get("monthly")), number(bandwidth.get("limit")))
        wl_used_str, wl_limit_str, wl_percent_str = format_usage(number(bandwidth.get("wl_monthly")), number(bandwidth.get("wl_limit")))
        text = f"""📈 **График трафика за {days} дней**

**Пользователь:** `{username}` ({obj.get('displayname')})

**Общий трафик:**
├ Upload: {fmt_bytes(number(total.get('upload')))}
├ Download: {fmt_bytes(number(total.get('download')))}
├ Использовано: {used_str} / {limit_str}
└ Процент: {percent_str}

**WL трафик:**
├ Upload: {fmt_bytes(number(wl_total.get('upload')))}
├ Download: {fmt_bytes(number(wl_total.get('download')))}
├ Использовано: {wl_used_str} / {wl_limit_str}
└ Процент: {wl_percent_str}"""
        text = truncate_utf8(text, 2000)
        history_raw = history.obj
        items = object_rows(history_raw)
        snapshots: list[BandwidthSnapshot] = [
            BandwidthSnapshot(
                ts=int(number(item.get("ts"))),
                up=int(number(item.get("up"))),
                down=int(number(item.get("down"))),
                wl_up=int(number(item.get("wl_up"))),
                wl_down=int(number(item.get("wl_down"))),
            )
            for item in items
        ]
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
