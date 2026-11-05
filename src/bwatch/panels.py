from __future__ import annotations

import time

from ._base import WatchJobs


class PanelJobs(WatchJobs):
    """Panel reconciliation and health alerts."""

    async def reconcile_inbounds(self) -> None:
        async def operation() -> None:
            failures = await self.sub.business_svc.reconcile()
            if failures:
                await self._alert_admin(
                    f"⚠️ Inbound reconcile failed for {len(failures)} user(s): {', '.join(failures[:10])}",
                )

        await self._job("reconcile", operation)

    async def panel_health_check(self) -> None:
        async def operation() -> None:
            live = [p for p in self.sub.panels if not p.dead]
            for panel, status in zip(
                live,
                await self.sub.panel_svc.statuses(live, work="background"),
                strict=True,
            ):
                if status is None:
                    continue
                obj = status.obj
                obj.format()
                problems: list[str] = []
                if obj.xray.state != "running":
                    problems.append(f"Xray: {obj.xray.state} - {obj.xray.errorMsg}")
                if obj.cpu > 90:
                    problems.append(f"CPU: {obj.cpu}%")
                for label, value in (("RAM", obj.mem), ("Disk", obj.disk)):
                    if value.total and value.current / value.total > 0.9:
                        problems.append(f"{label}: {value.current / value.total * 100:.0f}%")
                if not problems:
                    self._panel_alerts.pop(panel.name, None)
                elif (
                    time.monotonic() - self._panel_alerts.get(panel.name, float("-inf"))
                    > (self.cfg.view()["panel_alert_cooldown"])
                ):
                    await self._alert_admin(
                        f"⚠️ Panel {panel.name}:\n" + "\n".join(f"- {p}" for p in problems),
                    )
                    self._panel_alerts[panel.name] = time.monotonic()

        await self._job("health", operation)
