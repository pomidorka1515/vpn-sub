"""Administrator panel status workflows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

from custom_types import ServerMetricsResponse
from util import fmt_time

from ..composition import AdminFeatureMixin

__all__ = ["AdminPanelsMixin"]


if TYPE_CHECKING:
    from session import XUiSession


class _UNFETCHED_SENTINEL:
    def __repr__(self) -> str:
        return "<unfetched panel>"


_UNFETCHED: Final[_UNFETCHED_SENTINEL] = _UNFETCHED_SENTINEL()


class AdminPanelsMixin(AdminFeatureMixin):
    """Panel status and aggregation workflows."""

    def _cb_panel_info(
        self,
        chat_id: int,
        panel: XUiSession,
        last: bool,
        info: ServerMetricsResponse | _UNFETCHED_SENTINEL | None = _UNFETCHED,
    ) -> None:
        if info is _UNFETCHED:
            info = self.sub.panel_svc.getstatus(panel)
        fetched = cast(ServerMetricsResponse | None, info)
        if fetched is None:
            self._send_message(
                chat_id,
                f"❌ Статус панели {panel.name} неизвестен",
                reply_markup=self.get_main_menu(),
            )
            return
        obj = fetched.obj
        obj.format()
        sys_up = fmt_time(obj.uptime)
        app_up = fmt_time(obj.appStats.uptime)
        xr = obj.xray
        xr_status = "🟢 Работает" if xr.state == "running" else f"🔴 {xr.errorMsg}"
        GB = 1024**3
        MB = 1024**2
        text = f"""📊 <b>Статус сервера {panel.name}</b>

🖥 <b>Система</b>
├ <b>CPU:</b> <code>{round(obj.cpu)}%</code> (<code>{obj.cpuCores}</code>/<code>{obj.logicalPro}</code> ядер, <code>{round(obj.cpuSpeedMhz)} MHz</code>)
├ <b>Load:</b> <code>{obj.loads[0]}</code> | <code>{obj.loads[1]}</code> | <code>{obj.loads[2]}</code>
├ <b>RAM:</b> <code>{obj.mem.current / GB:.2f} GB</code> / <code>{obj.mem.total / GB:.2f} GB</code>
└ <b>Uptime:</b> <code>{sys_up}</code>

💾 <b>Накопители</b>
├ <b>Диск:</b> <code>{obj.disk.current / GB:.2f} GB</code> / <code>{obj.disk.total / GB:.2f} GB</code>
└ <b>Swap:</b> <code>{obj.swap.current / GB:.2f} GB</code> / <code>{obj.swap.total / GB:.2f} GB</code>

🌐 <b>Сеть & IP</b>
├ <b>IPv4:</b> <code>{obj.publicIP.ipv4}</code>
├ <b>IPv6:</b> <code>{obj.publicIP.ipv6 or "Отключен"}</code>
├ <b>Соединения:</b> <code>{obj.tcpCount}</code> TCP / <code>{obj.udpCount}</code> UDP
├ <b>Скорость:</b> ⬇️ <code>{obj.netIO.down / MB:.2f} MB/s</code> | ⬆️ <code>{obj.netIO.up / MB:.2f} MB/s</code>
└ <b>Трафик:</b> ⬇️ <code>{obj.netTraffic.recv / GB:.2f} GB</code> | ⬆️ <code>{obj.netTraffic.sent / GB:.2f} GB</code>

⚡️ <b>Xray Core v{obj.xray.version}</b>
└ <b>Статус:</b> {xr_status}

🤖 <b>Other</b>
├ <b>Потоков:</b> <code>{obj.appStats.threads}</code>
├ <b>RAM:</b> <code>{obj.appStats.mem / MB:.2f} MB</code>
└ <b>Uptime:</b> <code>{app_up}</code>"""
        self.bot.send_message(
            chat_id, text, parse_mode="HTML", reply_markup=self.get_main_menu() if last else None
        )

    def _cb_all_panels_status(self, chat_id: int) -> None:
        msg = self.bot.send_message(chat_id, "⏳ Получение статуса панелей...")
        panels = list(self.sub.panels)
        statuses = self.sub.panel_svc.statuses(panels)
        for i, (panel, status) in enumerate(zip(panels, statuses, strict=True)):
            last = i == len(panels) - 1
            try:
                self._cb_panel_info(chat_id, panel, last, status)
            except Exception:
                self.log.exception("panel operation failed for %s", panel.name)
                self._send_message(chat_id, "❌ Внутренняя ошибка", parse_mode="HTML")
        self._delete_message(chat_id, msg.message_id)
