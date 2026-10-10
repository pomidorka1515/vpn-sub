from __future__ import annotations

from typing import TYPE_CHECKING, cast

from composition import AdminFeatureMixin
from payloads import number
from util import fmt_time

from .common import obj_map, result_obj

if TYPE_CHECKING:
    from collections.abc import Mapping

    import discord

__all__ = ["AdminPanelsMixin"]


def _as_loads(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else [0, 0, 0]


class AdminPanelsMixin(AdminFeatureMixin):
    def _metrics(self, payload: Mapping[str, object]) -> dict[str, object] | None:
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
        GB = 1024**3
        MB = 1024**2
        xr_state = xray.get("state")
        xr_status = "🟢 Работает" if xr_state == "running" else f"🔴 {xray.get('errorMsg')}"
        sys_up = fmt_time(int(number(obj.get("uptime"))))
        app_up = fmt_time(int(number(app_stats.get("uptime"))))
        load0 = loads[0] if len(loads) > 0 else 0
        load1 = loads[1] if len(loads) > 1 else 0
        load2 = loads[2] if len(loads) > 2 else 0
        return f"""📊 **Статус сервера {name}**

🖥 **Система**
├ **CPU:** `{round(number(obj.get("cpu")))}%` (`{obj.get("cpuCores")}`/`{obj.get("logicalPro")}` ядер, `{round(number(obj.get("cpuSpeedMhz")))} MHz`)
├ **Load:** `{load0}` | `{load1}` | `{load2}`
├ **RAM:** `{number(mem.get("current")) / GB:.2f} GB` / `{number(mem.get("total")) / GB:.2f} GB`
└ **Uptime:** `{sys_up}`

💾 **Накопители**
├ **Диск:** `{number(disk.get("current")) / GB:.2f} GB` / `{number(disk.get("total")) / GB:.2f} GB`
└ **Swap:** `{number(swap.get("current")) / GB:.2f} GB` / `{number(swap.get("total")) / GB:.2f} GB`

🌐 **Сеть & IP**
├ **IPv4:** `{public_ip.get("ipv4")}`
├ **IPv6:** `{public_ip.get("ipv6") or "Отключен"}`
├ **Соединения:** `{obj.get("tcpCount")}` TCP / `{obj.get("udpCount")}` UDP
├ **Скорость:** ⬇️ `{number(net_io.get("down")) / MB:.2f} MB/s` | ⬆️ `{number(net_io.get("up")) / MB:.2f} MB/s`
└ **Трафик:** ⬇️ `{number(net_traffic.get("recv")) / GB:.2f} GB` | ⬆️ `{number(net_traffic.get("sent")) / GB:.2f} GB`

⚡️ **Xray Core v{xray.get("version")}**
└ **Статус:** {xr_status}

🤖 **Other**
├ **Потоков:** `{app_stats.get("threads")}`
├ **RAM:** `{number(app_stats.get("mem")) / MB:.2f} MB`
└ **Uptime:** `{app_up}`"""

    async def _cb_all_panels_status(self, interaction: discord.Interaction) -> None:
        await self._defer(interaction)
        await self._respond(interaction, "⏳ Получение статуса панелей...")
        result = await self.http.panel_status()
        if not await self.consume_result(interaction, result):
            return
        raw: object = result_obj(result)
        if not isinstance(raw, dict) or not raw:
            await self._respond(
                interaction, "❌ Статус панелей неизвестен", view=self.main_menu_view()
            )
            return
        panels = obj_map(cast(object, raw))
        names = list(panels.keys())
        for index, name in enumerate(names):
            payload = panels.get(name)
            mapping = (
                obj_map(cast(object, payload))
                if isinstance(payload, dict)
                else {"status": "unknown"}
            )
            last = index == len(names) - 1
            await self._respond(
                interaction,
                self._panel_text(name, mapping),
                view=self.main_menu_view() if last else None,
            )
