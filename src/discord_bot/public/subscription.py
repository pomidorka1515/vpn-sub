from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
from discord_bot.composition import PublicFeatureMixin
from util import fmt_bytes, make_qr

if TYPE_CHECKING:
    from custom_types import UserInfo

__all__ = ["PublicSubscriptionMixin"]


class PublicSubscriptionMixin(PublicFeatureMixin):
    def _quota(self, lang: str, used: float, limit: float) -> str:
        unlimited = self.text(lang, "unlimited") or "Unlimited"
        if not limit:
            return f"*{unlimited}*"
        return f"{fmt_bytes(used)} / {limit} GB"

    def _info_text(self, lang: str, obj: UserInfo) -> str | None:
        t = self.TEXTS[lang]
        template = t.get("info_text")
        if not template:
            return None
        daystext = t["days_word"]
        expiry_i = obj.time
        if expiry_i:
            days_left = str((expiry_i - int(time.time())) // 86400)
            date_end = datetime.fromtimestamp(expiry_i, tz=UTC).strftime("%d.%m.%y %H:%M (UTC)")
            time_str = f"{days_left} {daystext} ({date_end})"
        else:
            time_str = t.get("lifetime", "Lifetime")
        bandwidth = obj.bandwidth
        total = bandwidth.total
        wl_total = bandwidth.wl_total
        monthly = bandwidth.monthly
        wl_monthly = bandwidth.wl_monthly
        limit = bandwidth.limit
        wl_limit = bandwidth.wl_limit
        upload = total.upload
        download = total.download
        wl_up = wl_total.upload
        wl_down = wl_total.download
        return template.format(
            username=obj.displayname,
            status="🟢" if obj.enabled else "🔴",
            wl_status="🟢" if obj.wl_enabled else "🔴",
            online="🟢" if obj.online else "🔴",
            total=fmt_bytes(int(upload) + int(download)),
            monthly=self._quota(lang, monthly, limit),
            wl_total=fmt_bytes(int(wl_up) + int(wl_down)),
            wl_monthly=self._quota(lang, wl_monthly, wl_limit),
            up=fmt_bytes(upload),
            down=fmt_bytes(download),
            wl_up=fmt_bytes(wl_up),
            wl_down=fmt_bytes(wl_down),
            days=time_str,
            fingerprint=obj.fingerprint,
        )

    async def send_info(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        token = self.sessions.token(uid)
        if not token:
            await self._reply_key(interaction, "not_logged_in")
            return
        obj = await self.sub.business_svc.get_info(await self.username(interaction))
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
        obj = await self.sub.business_svc.get_info(await self.username(interaction))
        link = obj.link
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        conf = self.cfg.view()
        qr_link = f"{conf['domain']}/{conf['uri'].strip('/')}?token={obj.token}&lang={lang}"
        qr = await self.rendering.run(make_qr, qr_link)
        view = discord.ui.View(timeout=None)
        content = t.get("get_sub_text")
        if link:
            if link.startswith(("http://", "https://")):
                view.add_item(discord.ui.Button(label=t.get("get_sub_btn_link", "Open"), url=link))
            happ = link if link.startswith("happ://") else f"happ://add/{link}"
            extra = t.get("get_sub_btn_happ", "Happ")
            content = f"{content}\n\n{extra}: `{happ}`" if content else f"{extra}: `{happ}`"
        file = discord.File(qr, filename="qr.png")
        await self._respond(interaction, content, view=view, file=file, ephemeral=True)

    async def cmd_sub(self, interaction: discord.Interaction) -> None:
        await self.send_sub(interaction)
