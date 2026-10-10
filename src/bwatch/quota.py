from __future__ import annotations

import time

from custom_types import BandwidthInfo, BandwidthUpdate, UserRecord
from errors import AppError
from notify import (
    Notice,
    Notifier,
    classify,
    restored,
)
from notify.kinds import Kind, Scope

from .host import BWatchHost


class QuotaMixin(BWatchHost):
    """Quota polling and expiry / near-limit notifications."""

    def _update_user(
        self, *, username: str, enable: bool | None = None,
        wl_enable: bool | None = None, timee: bool | None = None,
    ) -> bool:
        """Run a background user update. Returns False when it failed.

        Callers must not notify the user of a state change that never
        happened — the next cycle retries it.
        """
        try:
            self.sub.business_svc.update_user(
                username=username, enable=enable, wl_enable=wl_enable, timee=timee,
            )
            return True
        except AppError:
            self.log.exception("background user update failed")
            return False

    def _notifier(self) -> Notifier | None:
        bot = self.bot
        if bot is None:
            return None
        def send(tgid: int, text: str) -> None:
            bot.bot.send_message(tgid, text, parse_mode="HTML")
        return Notifier(
            texts=bot.TEXTS,
            send=send,
            language=bot.get_lang,
            log=self.log,
        )

    def _deliver(
        self,
        notifier: Notifier | None,
        username: str,
        telegram_id: int | None,
        notices: tuple[Notice, ...],
        seen: set[str],
    ) -> None:
        """Send notices, recording each one in ``seen`` only after it is accepted.

        ``seen`` is the cycle's stored-key set. ``Notifier`` mutates it in
        place, so the caller persists exactly what Telegram accepted.
        """
        if notifier is None or not notices:
            return
        for notice in notices:
            notifier.notify(username, telegram_id, notice, seen)

    def _disable_for(self, notice: Notice, username: str) -> bool:
        """Apply the panel disable a terminal notice describes.

        False means the disable did not happen, so the notice must not be sent.
        """
        if notice.kind is Kind.EXPIRED:
            return self._update_user(username=username, enable=False, timee=False)
        if notice.kind is Kind.TRAFFIC_EXHAUSTED:
            return self._update_user(username=username, enable=False, timee=True)
        if notice.kind is Kind.WHITELIST_EXHAUSTED:
            return self._update_user(username=username, wl_enable=False)
        return False

    def _announce_recoveries(
        self,
        recoveries: dict[str, tuple[Notice, ...]],
        states: dict[str, UserRecord],
    ) -> None:
        if not recoveries:
            return
        notifier = self._notifier()
        if notifier is None:
            return
        tgids = self.db.user_tgids()
        seen = self.db.notification_markers()
        before = set(seen)
        for username, notices in recoveries.items():
            if username not in states:
                continue
            mapped = tgids.get(username)
            telegram_id = int(mapped) if mapped is not None else None
            self._deliver(notifier, username, telegram_id, notices, seen)
        self.db.sync_notifications(before, seen)

    def _reenable_under_quota(self) -> dict[str, UserRecord]:
        """Turn time and under-quota sides back on, then announce recoveries.

        Returns the post-re-enable states. Callers must reuse them instead of
        listing users again.
        """
        states: dict[str, UserRecord] = {}
        recoveries: dict[str, tuple[Notice, ...]] = {}
        for state in self.sub.user_svc.list_user_states():
            i = state["username"]
            states[i] = state
            reenabled_time = False
            reenabled_main = False
            reenabled_wl = False
            expires_at = int(state['expires_at'])
            time_ok = expires_at == 0 or (expires_at - int(time.time())) >= 0
            if time_ok and not bool(state['enabled_time']):
                reenabled_time = self._update_user(username=i, enable=True, timee=True)
                state = self.sub.user_svc.get_user_state(i)  # re-read after the mutation
                states[i] = state

            main_required = int(state['bw_limit_gb']) != 0
            wl_required = int(state['wl_limit_gb']) != 0
            if main_required and int(state['bw_used']) < int(int(state['bw_limit_gb']) * 10**9) and not bool(state['enabled']):
                reenabled_main = self._update_user(username=i, enable=True)
            if wl_required and int(state['wl_used']) < int(int(state['wl_limit_gb']) * 10**9) and not bool(state['enabled_wl']):
                reenabled_wl = self._update_user(username=i, wl_enable=True)
            if reenabled_time or reenabled_main or reenabled_wl:
                recoveries[i] = restored(
                    main=reenabled_main,
                    whitelist=reenabled_wl,
                    time=reenabled_time,
                )

        # Re-enables already happened. A failed traffic read must not swallow
        # the message, and neither must a cycle with no usage delta.
        self._announce_recoveries(recoveries, states)
        return states

    def _read_traffic(
        self, states: dict[str, UserRecord],
    ) -> tuple[dict[str, BandwidthInfo], dict[str, BandwidthInfo]] | None:
        """Read one map per required side.

        None means a required read failed. Both maps are read before either
        is committed, so a partial delta is never recorded.
        """
        need_main = any(int(state['bw_limit_gb']) != 0 for state in states.values())
        need_wl = any(int(state['wl_limit_gb']) != 0 for state in states.values())
        main_map: dict[str, BandwidthInfo] = {}
        wl_map: dict[str, BandwidthInfo] = {}
        try:
            pool = self._panel_pool()
            if need_main:
                main_map = self.sub.bandwidth_svc.all_traffic(pool=pool)
            if need_wl:
                wl_map = self.sub.bandwidth_svc.all_traffic(whitelist=True, pool=pool)
        except Exception:
            self.log.exception("bandwidth poll failed")
            return None
        return main_map, wl_map

    def _commit_usage(
        self,
        states: dict[str, UserRecord],
        maps: tuple[dict[str, BandwidthInfo], dict[str, BandwidthInfo]],
    ) -> None:
        """Record positive deltas, then advance baselines and persist together."""
        main_map, wl_map = maps
        updates: dict[str, BandwidthUpdate] = {}
        wl_updates: dict[str, BandwidthUpdate] = {}
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

    def bandwidth_check(self) -> None:
        states = self._reenable_under_quota()
        maps = self._read_traffic(states)
        if maps is None:
            return
        self._commit_usage(states, maps)

    def check(self) -> None:
        tgids = self.db.user_tgids()
        notifier = self._notifier()
        seen = self.db.notification_markers()
        before = set(seen)
        now = int(time.time())
        for state in self.sub.user_svc.list_user_states():
            i = state["username"]
            mapped = tgids.get(i)
            tg_user = int(mapped) if mapped is not None else None
            notices = classify(state, now)
            applied: list[Notice] = []
            for notice in notices:
                if notice.kind.scope is Scope.EPISODE:
                    if not self._disable_for(notice, i):
                        continue
                applied.append(notice)
            self._deliver(notifier, i, tg_user, tuple(applied), seen)
        self.db.sync_notifications(before, seen)
