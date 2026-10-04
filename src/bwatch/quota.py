from __future__ import annotations

from typing import Any
import time

from custom_types import BandwidthInfo, BandwidthUpdate, UserRecord
from errors import AppError

from .host import BWatchHost


class QuotaMixin(BWatchHost):
    """Quota polling and expiry / near-limit notifications."""

    def _update_user(self, *args: Any, **kwargs: Any) -> bool:
        """Run a background user update. Returns False when it failed.

        Callers must not notify the user of a state change that never
        happened — the next cycle retries it.
        """
        try:
            self.sub.business_svc.update_user(*args, **kwargs)
            return True
        except AppError:
            self.log.error("background user update failed", exc_info=True)
            return False

    def bandwidth_check(self) -> None:
        updates: dict[str, BandwidthUpdate] = {}    # username -> (delta, current) for main
        wl_updates: dict[str, BandwidthUpdate] = {} # username -> (delta, current) for whitelist
        states: dict[str, UserRecord] = {}
        need_main = False
        need_wl = False
        for state in self.sub.user_svc.list_user_states():
            i = state["username"]
            states[i] = state
            # Main bandwidth
            expires_at = int(state['expires_at'])
            time_ok = expires_at == 0 or (expires_at - int(time.time())) >= 0
            if time_ok and not bool(state['enabled_time']):
                self._update_user(username=i, enable=True, timee=True)
                state = self.sub.user_svc.get_user_state(i)  # re-read after the mutation
                states[i] = state

            main_required = int(state['bw_limit_gb']) != 0
            wl_required = int(state['wl_limit_gb']) != 0
            need_main = need_main or main_required
            need_wl = need_wl or wl_required
            if main_required and int(state['bw_used']) < int(int(state['bw_limit_gb']) * 10**9) and not bool(state['enabled']):
                self._update_user(username=i, enable=True)
            if wl_required and int(state['wl_used']) < int(int(state['wl_limit_gb']) * 10**9) and not bool(state['enabled_wl']):
                self._update_user(username=i, wl_enable=True)

        # One batched read per side (clients/list per panel). Read both
        # before committing either: if either required read fails, commit
        # nothing this cycle so no partial delta is recorded.
        main_map: dict[str, BandwidthInfo] = {}
        wl_map: dict[str, BandwidthInfo] = {}
        try:
            pool = self._panel_pool()
            if need_main:
                main_map = self.sub.bandwidth_svc.all_traffic(pool=pool)
            if need_wl:
                wl_map = self.sub.bandwidth_svc.all_traffic(whitelist=True, pool=pool)
        except Exception:
            self.log.error("bandwidth poll failed", exc_info=True)
            return

        with self._mem_lock:
            for i, state in states.items():
                main_required = int(state['bw_limit_gb']) != 0
                wl_required = int(state['wl_limit_gb']) != 0
                if main_required:
                    current_bws = main_map.get(i, BandwidthInfo(0, 0, 0))
                    if i not in self.mem:
                        self.mem[i] = current_bws
                    else:
                        delta = int(current_bws.total - self.mem[i].total)
                        if delta > 0:
                            updates[i] = BandwidthUpdate(delta=delta, current=current_bws)
                if wl_required:
                    current_wl_bws = wl_map.get(i, BandwidthInfo(0, 0, 0))
                    if i not in self.wl_mem:
                        self.wl_mem[i] = current_wl_bws
                    else:
                        delta = int(current_wl_bws.total - self.wl_mem[i].total)
                        if delta > 0:
                            wl_updates[i] = BandwidthUpdate(delta=delta, current=current_wl_bws)

        if not updates and not wl_updates:
            return

        usage: dict[str, tuple[int, int]] = {}
        with self._mem_lock:
            for i, update in updates.items():
                usage[i] = (update.delta, 0)
                self.mem[i] = update.current
            for i, update in wl_updates.items():
                regular, _whitelist = usage.get(i, (0, 0))
                usage[i] = (regular, update.delta)
                self.wl_mem[i] = update.current
        self.db.increment_usages(usage)

    def check(self) -> None:
        tgids = self.db.user_tgids()
        for state in self.sub.user_svc.list_user_states():
            i = state["username"]
            mapped = tgids.get(i)
            tg_user = int(mapped) if mapped is not None else None
            expires_at = int(state['expires_at'])
            bw_limit = int(state['bw_limit_gb'])
            bw_used = int(state['bw_used'])
            wl_limit = int(state['wl_limit_gb'])
            wl_used = int(state['wl_used'])
            if expires_at != 0:
                if (expires_at - int(time.time())) <= 0:
                    if bool(state['enabled_time']):
                        disabled = self._update_user(username=i, enable=False, timee=False)
                        if disabled and self.bot: self.bot.msg(tg_user, 'warning_disabled') # sub expired
                    continue
                else:
                    days = (expires_at - int(time.time())) // 86400
                    if days <= 2 and tg_user is not None and self.db.mark_notification("regular", tg_user):
                            if self.bot: self.bot.msg(tg_user, 'warning_days', days=days)
            if wl_limit != 0 and wl_used > int(wl_limit * 10**9):
                if bool(state['enabled_wl']):
                    disabled = self._update_user(username=i, wl_enable=False)
                    if disabled and self.bot: self.bot.msg(tg_user, 'warning_traffic_whitelist_disabled', available=wl_limit)
            elif wl_limit != 0 and wl_used > int(wl_limit * 10**9 * 0.95) and tg_user is not None and self.db.mark_notification("whitelist", tg_user):
                    if self.bot: self.bot.msg(tg_user, 'warning_traffic_whitelist', used=int(round(wl_used / 10**6, 0)), available=wl_limit)
            if not bool(state['enabled']):
                continue
            if bw_limit == 0:
                continue
            if bw_used > int(bw_limit * 10**9):
                disabled = self._update_user(username=i, enable=False, timee=True)
                if disabled and self.bot: self.bot.msg(tg_user, 'warning_traffic_disabled', available=bw_limit)
            elif bw_used > int(bw_limit * 10**9 * 0.95) and tg_user is not None and self.db.mark_notification("regular", tg_user):
                    if self.bot: self.bot.msg(tg_user, 'warning_traffic', used=int(round(bw_used / 10**6, 0)), available=bw_limit)
