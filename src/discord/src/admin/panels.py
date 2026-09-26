from __future__ import annotations

from typing import Any, Mapping, cast

import discord

from composition import AdminFeatureMixin
from util import fmt_time
from .common import obj_map, result_obj

__all__ = ["AdminPanelsMixin"]


def _as_loads(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else [0, 0, 0]


class AdminPanelsMixin(AdminFeatureMixin):
    def _metrics(self, payload: Mapping[str, object]) -> dict[str, Any] | None:
        if payload.get("status") == "unknown":
            return None
        # GET /api/panel/status stores the 3x-ui envelope in obj:
        # {success, msg, obj: {cpu, mem, ...}}. Older callers may already
        # pass the inner metrics object.
        nested = payload.get("obj")
        if isinstance(nested, dict):
            envelope = obj_map(cast(object, nested))
            if envelope.get("success") is False and "obj" not in envelope and "cpu" not in envelope:
                return None
            inner = envelope.get("obj")
            if isinstance(inner, dict):
                return obj_map(cast(object, inner))
            if "cpu" in envelope or "mem" in envelope:
                return envelope
            return None
        if "cpu" in payload or "mem" in payload:
            return obj_map(cast(object, payload))
        return None

    def _panel_text(self, name: str, payload: Mapping[str, object]) -> str:
        obj = self._metrics(payload)
        if obj is None:
            return f"❌ Статус панели {name} неизвестен"
        mem = obj_map(obj.get("mem"))
        swap = obj_map(obj.get("swap"))
        disk = obj_map(obj.get("disk"))
        net_io = obj_map(obj.get("netIO"))
        net_traffic = obj_map(obj.get("netTraffic"))
        public_ip = obj_map(obj.get("publicIP"))
        app_stats = obj_map(obj.get("appStats"))
        xray = obj_map(obj.get("xray"))
        loads = _as_loads(obj.get("loads"))
        GB = 1024 ** 3
        MB = 1024 ** 2
        xr_state = xray.get("state")
        xr_status = "🟢 Работает" if xr_state == "running" else f"🔴 {xray.get('errorMsg')}"
        sys_up = fmt_time(int(obj.get("uptime") or 0))
        app_up = fmt_time(int(app_stats.get("uptime") or 0))
        load0 = loads[0] if len(loads) > 0 else 0
        load1 = loads[1] if len(loads) > 1 else 0
        load2 = loads[2] if len(loads) > 2 else 0
        return f"""📊 **Статус сервера {name}**

🖥 **Система**
├ **CPU:** `{int(round(float(obj.get('cpu') or 0)))}%` (`{obj.get('cpuCores')}`/`{obj.get('logicalPro')}` ядер, `{int(round(float(obj.get('cpuSpeedMhz') or 0)))} MHz`)
├ **Load:** `{load0}` | `{load1}` | `{load2}`
├ **RAM:** `{float(mem.get('current') or 0) / GB:.2f} GB` / `{float(mem.get('total') or 0) / GB:.2f} GB`
└ **Uptime:** `{sys_up}`

💾 **Накопители**
├ **Диск:** `{float(disk.get('current') or 0) / GB:.2f} GB` / `{float(disk.get('total') or 0) / GB:.2f} GB`
└ **Swap:** `{float(swap.get('current') or 0) / GB:.2f} GB` / `{float(swap.get('total') or 0) / GB:.2f} GB`

🌐 **Сеть & IP**
├ **IPv4:** `{public_ip.get('ipv4')}`
├ **IPv6:** `{public_ip.get('ipv6') or 'Отключен'}`
├ **Соединения:** `{obj.get('tcpCount')}` TCP / `{obj.get('udpCount')}` UDP
├ **Скорость:** ⬇️ `{float(net_io.get('down') or 0) / MB:.2f} MB/s` | ⬆️ `{float(net_io.get('up') or 0) / MB:.2f} MB/s`
└ **Трафик:** ⬇️ `{float(net_traffic.get('recv') or 0) / GB:.2f} GB` | ⬆️ `{float(net_traffic.get('sent') or 0) / GB:.2f} GB`

⚡️ **Xray Core v{xray.get('version')}**
└ **Статус:** {xr_status}

🤖 **Other**
├ **Потоков:** `{app_stats.get('threads')}`
├ **RAM:** `{float(app_stats.get('mem') or 0) / MB:.2f} MB`
└ **Uptime:** `{app_up}`"""

    async def _cb_all_panels_status(self, interaction: discord.Interaction) -> None:
        await self._defer(interaction)
        await self._respond(interaction, "⏳ Получение статуса панелей...")
        result = await self.http.panel_status()
        if not await self.consume_result(interaction, result):
            return
        raw: object = result_obj(result)
        if not isinstance(raw, dict) or not raw:
            await self._respond(interaction, "❌ Статус панелей неизвестен", view=self.main_menu_view())
            return
        panels = obj_map(cast(object, raw))
        names = list(panels.keys())
        for index, name in enumerate(names):
            payload = panels.get(name)
            mapping = obj_map(cast(object, payload)) if isinstance(payload, dict) else {"status": "unknown"}
            last = index == len(names) - 1
            await self._respond(
                interaction,
                self._panel_text(name, mapping),
                view=self.main_menu_view() if last else None,
            )
