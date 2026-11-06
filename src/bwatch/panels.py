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
            statuses = dict(
                zip(
                    (panel.name for panel in live),
                    await self.sub.panel_svc.statuses(live, work="background") if live else [],
                    strict=True,
                ),
            )
            for panel in self.sub.panels:
                status = statuses.get(panel.name)
                problems: list[str] = []
                if panel.dead:
                    problems.append("Unavailable: panel session is down")
                elif status is None:
                    problems.append("Unknown: panel status could not be read")
                else:
                    obj = status.obj
                    obj.format()
                    if obj.xray.state != "running":
                        problems.append(f"Xray: {obj.xray.state} - {obj.xray.errorMsg}")
                    if obj.cpu > 90:
                        problems.append(f"CPU: {obj.cpu}%")
                    for label, value in (("RAM", obj.mem), ("Disk", obj.disk)):
                        if value.total and value.current / value.total > 0.9:
                            problems.append(f"{label}: {value.current / value.total * 100:.0f}%")
                if not problems:
                    # Only confirmed healthy metrics end an alert episode. Recovery
                    # is silent and re-arms the next fault, even within cooldown.
                    self._panel_alerts.pop(panel.name, None)
                    self._panel_alert_retries.pop(panel.name, None)
                elif (
                    time.monotonic() - self._panel_alerts.get(panel.name, float("-inf"))
                    > (self.cfg.view()["panel_alert_cooldown"])
                ):
                    failures, due = self._panel_alert_retries.get(panel.name, (0, 0.0))
                    if time.monotonic() < due:
                        continue
                    # Cancellation also retains a retry delay, never a success
                    # cooldown. Cap both state and delay; retries run on
                    # subsequent health checks, not separate tasks.
                    failures = min(failures + 1, 4)
                    accepted = False
                    try:
                        accepted = await self._alert_admin(
                            f"⚠️ Panel {panel.name}:\n" + "\n".join(f"- {p}" for p in problems),
                        )
                    finally:
                        if not accepted:
                            self._panel_alert_retries[panel.name] = (
                                failures,
                                time.monotonic() + min(60 * 2 ** (failures - 1), 300),
                            )
                    if accepted:
                        self._panel_alerts[panel.name] = time.monotonic()
                        self._panel_alert_retries.pop(panel.name, None)

        await self._job("health", operation)
