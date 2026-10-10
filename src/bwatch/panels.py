from __future__ import annotations

import time

from errors import AppError

from .host import BWatchHost


class PanelsMixin(BWatchHost):
    """Panel health alerts and inbound reconcile."""

    def panel_health_check(self) -> None:
        """Check each panel's Xray status and resource usage. Alert on issues."""
        live = [panel for panel in self.sub.panels if not panel.dead]
        # Dead panels stay out so they do not pick up getstatus's error log.
        # Alert math stays on this thread; _panel_alerts is single-threaded.
        for panel, status in zip(
            live, self.sub.panel_svc.statuses(live, self._panel_pool()), strict=True
        ):
            try:
                if not status:
                    continue

                key = panel.name
                problems: list[str] = []
                obj = status.obj
                obj.format()

                xray = obj.xray
                if xray.state != "running":
                    problems.append(f"Xray: {xray.state} - {xray.errorMsg}")

                cpu = obj.cpu
                if cpu > 90:
                    problems.append(f"CPU: {cpu}%")

                mem = obj.mem
                if mem.total > 0:
                    mem_pct = (mem.current / mem.total) * 100
                    if mem_pct > 90:
                        problems.append(f"RAM: {mem_pct:.0f}%")

                disk = obj.disk
                if disk.total > 0:
                    disk_pct = (disk.current / disk.total) * 100
                    if disk_pct > 90:
                        problems.append(f"Disk: {disk_pct:.0f}%")

                if problems:
                    last = self._panel_alerts.get(key, 0)
                    if time.time() - last > self._panel_alert_cooldown:
                        self._panel_alerts[key] = time.time()
                        if self.admin_bot:
                            msg = f"⚠️ Panel {key}:\n" + "\n".join(f"- {p}" for p in problems)
                            self.admin_bot.msg(msg)
                else:
                    self._panel_alerts.pop(key, None)
            except Exception:
                self.log.exception(
                    "health check failed for panel %s (%s)", panel.name, panel.address
                )

    def reconcile_inbounds(self) -> None:
        """Re-sync every user to every panel (idempotent).

        Creates missing panel clients and attaches any VLESS inbounds the
        existing clients lack (e.g. after an admin adds an inbound to a
        panel). One user's panel rejection must not abort the rest; the
        next cycle retries the failures.

        One client list per panel for the whole cycle, filled here — not one
        ``clients/get`` per user. Users stay serial: each ``add_users``
        already waits on the panel pool, and a user worker on that same pool
        would deadlock.
        """
        failures: list[str] = []
        known = self.sub.panel_svc.client_maps(self.sub.panels)
        for username in self.sub.user_svc.list_users():
            try:
                self.sub.business_svc.add_users(
                    username,
                    _called_internally=True,
                    known_clients=known,
                )
            except AppError:
                self.log.exception("inbound reconcile failed for %s", username)
                failures.append(username)
        if failures:
            shown = ", ".join(failures[:10])
            self._alert_admin(f"⚠️ Inbound reconcile failed for {len(failures)} user(s): {shown}")
