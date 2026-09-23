from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, cast

import discord

from chart import bandwidth_chart
from composition import PublicFeatureMixin
from custom_types import BandwidthSnapshot
from util import fmt_bytes, format_usage, truncate_utf8

__all__ = ["PublicTrafficMixin"]


class PublicTrafficMixin(PublicFeatureMixin):
    def chart_view(self, lang: str) -> discord.ui.View:
        t = self.TEXTS[lang]
        view = discord.ui.View(timeout=None)
        options = [
            discord.SelectOption(label=t["btn_chart_days"].format(days=days), value=str(days))
            for days in (3, 14, 30, 90)
        ]
        view.add_item(discord.ui.Select(custom_id="chart_select", options=options, min_values=1, max_values=1))
        return view

    def _chart_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._chart_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chart_locks[user_id] = lock
        return lock

    async def cmd_chart(self, interaction: discord.Interaction) -> None:
        lang = self.get_lang(interaction.user.id)
        await self._reply_key(interaction, "choose_chart_days", view=self.chart_view(lang))

    async def render_chart(self, interaction: discord.Interaction, days: int) -> None:
        uid = interaction.user.id
        token = self.sessions.token(uid)
        if not token:
            await self._reply_key(interaction, "not_logged_in")
            return
        if not 1 <= days <= 90:
            return
        busy = self._chart_busy
        if uid in busy:
            await self._reply_key(interaction, "chart_generating")
            return
        busy.add(uid)
        lock = self._chart_lock(uid)
        try:
            async with lock:
                await self._defer(interaction, ephemeral=True)
                await self._reply_key(interaction, "chart_generating")
                history = await self.http.history(token, days)
                if not await self.consume_result(interaction, history):
                    return
                stats = await self.http.stats(token)
                if not await self.consume_result(interaction, stats):
                    return
                lang = self.get_lang(uid)
                t = self.TEXTS[lang]
                raw_obj = stats.obj
                if not isinstance(raw_obj, dict):
                    await self._reply_key(interaction, "bad_response")
                    return
                obj = cast(dict[str, Any], raw_obj)
                bandwidth_obj = obj.get("bandwidth")
                if isinstance(bandwidth_obj, dict):
                    bandwidth = cast(dict[str, Any], bandwidth_obj)
                else:
                    bandwidth = {}
                total_obj = bandwidth.get("total")
                if isinstance(total_obj, dict):
                    total = cast(dict[str, Any], total_obj)
                else:
                    total = {}
                wl_total_obj = bandwidth.get("wl_total")
                if isinstance(wl_total_obj, dict):
                    wl_total = cast(dict[str, Any], wl_total_obj)
                else:
                    wl_total = {}
                used_str, limit_str, percent_str = format_usage(
                    bandwidth.get("monthly") or 0,
                    bandwidth.get("limit") or 0,
                    t.get("unlimited", "Unlimited"),
                )
                wl_used_str, wl_limit_str, wl_percent_str = format_usage(
                    bandwidth.get("wl_monthly") or 0,
                    bandwidth.get("wl_limit") or 0,
                    t.get("unlimited", "Unlimited"),
                )
                text = t.get("chart_text", "").format(
                    days=days,
                    upload=fmt_bytes(total.get("upload") or 0),
                    download=fmt_bytes(total.get("download") or 0),
                    used=used_str,
                    limit=limit_str,
                    percent=percent_str,
                    wl_upload=fmt_bytes(wl_total.get("upload") or 0),
                    wl_download=fmt_bytes(wl_total.get("download") or 0),
                    wl_used=wl_used_str,
                    wl_limit=wl_limit_str,
                    wl_percent=wl_percent_str,
                )
                text = truncate_utf8(text, 1024)
                snapshots: list[BandwidthSnapshot] = []
                history_raw = history.obj
                if isinstance(history_raw, list):
                    raw_history = cast(list[dict[str, Any]], history_raw)
                else:
                    raw_history = []
                for item in raw_history:
                    snapshots.append(
                        BandwidthSnapshot(
                            ts=int(item.get("ts") or 0),
                            up=int(item.get("up") or 0),
                            down=int(item.get("down") or 0),
                            wl_up=int(item.get("wl_up") or 0),
                            wl_down=int(item.get("wl_down") or 0),
                        )
                    )
                chart_lang = {
                    "bandwidth": "Bandwidth",
                    "days": "days",
                    "day": "day",
                    "regular_traffic": "Regular traffic",
                    "whitelist_traffic": "Whitelist traffic",
                    "download": "Download",
                    "upload": "Upload",
                    "no_data": "No data",
                }
                if lang == "ru":
                    chart_lang = {
                        "bandwidth": "Использование трафика",
                        "days": "дн.",
                        "day": "день",
                        "regular_traffic": "Обычный трафик",
                        "whitelist_traffic": "Белый список",
                        "download": "Загрузка",
                        "upload": "Отдача",
                        "no_data": "Нет данных",
                    }
                image = await asyncio.to_thread(
                    bandwidth_chart,
                    snapshots,
                    label=str(obj.get("displayname") or ""),
                    lang=cast(Mapping[str, str], chart_lang),
                )
                file: discord.File | None = None
                if image is not None:
                    image.seek(0)
                    file = discord.File(image, filename="chart.png")
                await self._respond(interaction, text or None, file=file)
        finally:
            busy.discard(uid)
