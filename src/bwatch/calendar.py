from __future__ import annotations

from datetime import datetime, UTC

from .host import BWatchHost


class CalendarMixin(BWatchHost):
    """Monthly quota reset.

    Notification markers are cleared inside ``reset_monthly``, when usage
    actually resets. A daily wipe would re-send episode warnings to users
    who are still disabled.
    """

    def is_first(self) -> None:
        # NOTE: This function is NOT meant to be called like `bwatch_instance.is_first()`.
        # NOTE: Exclusive to one thread only.
        now = datetime.now(UTC)
        if now.day != 1:
            return
        today = now.strftime("%Y-%m-%d")

        # Restart-safe: compare against stored month key, not just the date string.
        # '_last_reset_month' stores "YYYY-MM" so a process restart on day 2
        # doesn't accidentally re-trigger a reset that already happened.
        current_month = now.strftime("%Y-%m")
        self.db.reset_monthly(current_month, today)
