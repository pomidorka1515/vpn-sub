from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import discord
from chart import bandwidth_chart
from discord_bot.composition import PublicFeatureMixin
from util import fmt_bytes, format_usage, truncate_utf8

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["PublicTrafficMixin"]


class PublicTrafficMixin(PublicFeatureMixin):
    def _chart_lang(self, lang: str) -> Mapping[str, str]:
        language = self.lang_cfg.view()
        raw = cast(object, language.get("chart"))
        if isinstance(raw, dict):
            tables = cast(dict[object, object], raw)
            table = tables.get(lang)
            if not isinstance(table, dict):
                table = tables.get("en")
            if isinstance(table, dict):
                return {str(k): str(v) for k, v in cast(dict[object, object], table).items()}
        return {}

    def chart_view(self, lang: str) -> discord.ui.View:
        t = self.TEXTS[lang]
        view = discord.ui.View(timeout=None)
        options = [
            discord.SelectOption(label=t["btn_chart_days"].format(days=days), value=str(days))
            for days in (3, 14, 30, 90)
        ]
        view.add_item(
            discord.ui.Select(
                custom_id="chart_select",
                options=options,
                min_values=1,
                max_values=1,
            ),
        )
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
                await self._defer(interaction)
                await self._reply_key(interaction, "chart_generating")
                username = await self.username(interaction)
                snapshots = await self.sub.bandwidth_svc.get_bw_history(username, days)
                obj = await self.sub.business_svc.get_info(username)
                lang = self.get_lang(uid)
                t = self.TEXTS[lang]
                bandwidth = obj.bandwidth
                total = bandwidth.total
                wl_total = bandwidth.wl_total
                used_str, limit_str, percent_str = format_usage(
                    bandwidth.monthly,
                    bandwidth.limit,
                    t.get("unlimited", "Unlimited"),
                )
                wl_used_str, wl_limit_str, wl_percent_str = format_usage(
                    bandwidth.wl_monthly,
                    bandwidth.wl_limit,
                    t.get("unlimited", "Unlimited"),
                )
                text = t.get("chart_text", "").format(
                    days=days,
                    upload=fmt_bytes(total.upload),
                    download=fmt_bytes(total.download),
                    used=used_str,
                    limit=limit_str,
                    percent=percent_str,
                    wl_upload=fmt_bytes(wl_total.upload),
                    wl_download=fmt_bytes(wl_total.download),
                    wl_used=wl_used_str,
                    wl_limit=wl_limit_str,
                    wl_percent=wl_percent_str,
                )
                text = truncate_utf8(text, 1024)
                chart_lang = self._chart_lang(lang)
                image = await self.rendering.run(
                    bandwidth_chart,
                    snapshots,
                    label=obj.displayname,
                    lang=chart_lang,
                )
                file: discord.File | None = None
                if image is not None:
                    image.seek(0)
                    file = discord.File(image, filename="chart.png")
                await self._respond(interaction, text or None, file=file)
        finally:
            busy.discard(uid)
