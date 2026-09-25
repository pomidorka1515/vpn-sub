from __future__ import annotations

import io
import time
from datetime import datetime, timezone
from typing import Any, Mapping, cast

import discord

from composition import PublicFeatureMixin
from util import fmt_bytes

__all__ = ["PublicSubscriptionMixin"]


class PublicSubscriptionMixin(PublicFeatureMixin):
    def _quota(self, lang: str, used: int | float, limit: int | float) -> str:
        unlimited = self.text(lang, "unlimited") or "Unlimited"
        if not limit:
            return f"*{unlimited}*"
        return f"{fmt_bytes(used)} / {limit} GB"

    def _info_text(self, lang: str, obj: Mapping[str, Any]) -> str | None:
        t = self.TEXTS[lang]
        template = t.get("info_text")
        if not template:
            return None
        daystext = "дней" if lang == "ru" else "days"
        expiry = obj.get("time") or 0
        try:
            expiry_i = int(expiry)
        except (TypeError, ValueError):
            expiry_i = 0
        if expiry_i:
            days_left = str((expiry_i - int(time.time())) // 86400)
            date_end = datetime.fromtimestamp(expiry_i, tz=timezone.utc).strftime("%d.%m.%y %H:%M (UTC)")
            time_str = f"{days_left} {daystext} ({date_end})"
        else:
            time_str = t.get("lifetime", "Lifetime")
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
        monthly = bandwidth.get("monthly") or 0
        wl_monthly = bandwidth.get("wl_monthly") or 0
        limit = bandwidth.get("limit") or 0
        wl_limit = bandwidth.get("wl_limit") or 0
        upload = total.get("upload") or 0
        download = total.get("download") or 0
        wl_up = wl_total.get("upload") or 0
        wl_down = wl_total.get("download") or 0
        return template.format(
            username=obj.get("displayname") or "",
            status="🟢" if obj.get("enabled") else "🔴",
            wl_status="🟢" if obj.get("wl_enabled") else "🔴",
            online="🟢" if obj.get("online") else "🔴",
            total=fmt_bytes(int(upload) + int(download)),
            monthly=self._quota(lang, monthly, limit),
            wl_total=fmt_bytes(int(wl_up) + int(wl_down)),
            wl_monthly=self._quota(lang, wl_monthly, wl_limit),
            up=fmt_bytes(upload),
            down=fmt_bytes(download),
            wl_up=fmt_bytes(wl_up),
            wl_down=fmt_bytes(wl_down),
            days=time_str,
            fingerprint=obj.get("fingerprint") or "",
        )

    async def send_info(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        token = self.sessions.token(uid)
        if not token:
            await self._reply_key(interaction, "not_logged_in")
            return
        result = await self.http.stats(token)
        if not await self.consume_result(interaction, result):
            return
        raw_obj = result.obj
        if not isinstance(raw_obj, dict):
            await self._reply_key(interaction, "bad_response")
            return
        obj = cast(dict[str, object], raw_obj)
        lang = self.get_lang(uid)
        text = self._info_text(lang, obj)
        if text:
            await self._respond(interaction, text)

    async def cmd_info(self, interaction: discord.Interaction) -> None:
        await self._defer(interaction)
        await self.send_info(interaction)

    async def send_sub(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        token = self.sessions.token(uid)
        if not token:
            await self._reply_key(interaction, "not_logged_in")
            return
        await self._defer(interaction, ephemeral=True)
        stats = await self.http.stats(token)
        if not await self.consume_result(interaction, stats):
            return
        raw_obj = stats.obj
        if not isinstance(raw_obj, dict):
            await self._reply_key(interaction, "bad_response")
            return
        obj = cast(dict[str, object], raw_obj)
        link = str(obj.get("link") or "")
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        qr = await self.http.qr(token, happ=False, lang=lang)
        view = discord.ui.View(timeout=None)
        content = t.get("get_sub_text")
        if link:
            if link.startswith(("http://", "https://")):
                view.add_item(discord.ui.Button(label=t.get("get_sub_btn_link", "Open"), url=link))
            happ = link if link.startswith("happ://") else f"happ://add/{link}"
            extra = t.get("get_sub_btn_happ", "Happ")
            content = f"{content}\n\n{extra}: `{happ}`" if content else f"{extra}: `{happ}`"
        file: discord.File | None = None
        if qr.ok and qr.body:
            file = discord.File(io.BytesIO(qr.body), filename="qr.png")
        await self._respond(interaction, content, view=view, file=file, ephemeral=True)

    async def cmd_sub(self, interaction: discord.Interaction) -> None:
        await self.send_sub(interaction)
