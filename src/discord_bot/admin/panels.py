from __future__ import annotations

from typing import TYPE_CHECKING

from discord_bot.composition import AdminFeatureMixin
from util import fmt_bytes, fmt_time

if TYPE_CHECKING:
    import discord
    from custom_types import ServerMetricsResponse

__all__ = ["AdminPanelsMixin"]


class AdminPanelsMixin(AdminFeatureMixin):
    def _panel_text(self, name: str, payload: ServerMetricsResponse | None) -> str:
        if payload is None or not payload.success:
            return f"❌ Статус панели {name} неизвестен"
        obj = payload.obj
        xray = "🟢 Работает" if obj.xray.state == "running" else f"🔴 {obj.xray.errorMsg}"
        return f"""📊 **Статус сервера {name}**

🖥 **Система**
CPU: `{round(obj.cpu)}%` (`{obj.cpuCores}`/`{obj.logicalPro}` ядер, `{round(obj.cpuSpeedMhz)} MHz`)
Load: {" | ".join(str(load) for load in obj.loads)}
RAM: {fmt_bytes(obj.mem.current)} / {fmt_bytes(obj.mem.total)}
Uptime: {fmt_time(obj.uptime)}

💾 **Накопители**
Диск: {fmt_bytes(obj.disk.current)} / {fmt_bytes(obj.disk.total)}
Swap: {fmt_bytes(obj.swap.current)} / {fmt_bytes(obj.swap.total)}

🌐 **Сеть & IP**
IPv4: `{obj.publicIP.ipv4}`
IPv6: `{obj.publicIP.ipv6 or "Отключен"}`
Соединения: `{obj.tcpCount}` TCP / `{obj.udpCount}` UDP
Скорость: ⬇️ {fmt_bytes(obj.netIO.down)}/s | ⬆️ {fmt_bytes(obj.netIO.up)}/s
Трафик: ⬇️ {fmt_bytes(obj.netTraffic.recv)} | ⬆️ {fmt_bytes(obj.netTraffic.sent)}

⚡️ **Xray Core v{obj.xray.version}**
Статус: {xray}

🤖 **Other**
Потоков: `{obj.appStats.threads}`
RAM: {fmt_bytes(obj.appStats.mem)}
Uptime: {fmt_time(obj.appStats.uptime)}"""

    async def _cb_all_panels_status(self, interaction: discord.Interaction) -> None:
        await self._defer(interaction)
        await self._respond(interaction, "⏳ Получение статуса панелей...")
        statuses = await self.sub.panel_svc.statuses()
        if not statuses:
            await self._respond(
                interaction, "❌ Статус панелей неизвестен", view=self.main_menu_view()
            )
            return
        for index, (panel, status) in enumerate(zip(self.sub.panels, statuses, strict=True)):
            await self._respond(
                interaction,
                self._panel_text(panel.name, status),
                view=self.main_menu_view() if index == len(statuses) - 1 else None,
            )
