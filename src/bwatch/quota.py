from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import TYPE_CHECKING

from errors import AppError
from notify import Notice, classify, restored
from notify.kinds import Kind, Scope

from ._base import WatchJobs

if TYPE_CHECKING:
    from custom_types import UserRecord


class QuotaJobs(WatchJobs):
    """Quota transitions and durable notification delivery."""

    async def _deliver(self, state: UserRecord, notices: tuple[Notice, ...]) -> None:
        if self.notifier is None:
            return
        # The account coordinator covers read/send/persist, including cancellation.
        storage_key = f"user_notifications_pending:{state['username']}"
        raw = await self.db.get_metadata(storage_key)
        if raw is None and not notices:
            return
        seen = await self.db.notification_markers()
        before = set(seen)
        pending: dict[str, Notice] = {}
        if raw is not None:
            saved = json.loads(raw)
            if saved.get("uuid") == state["uuid"]:
                for item in saved["notices"]:
                    notice = Notice(
                        Kind[item["kind"]],
                        item["bucket"],
                        item["fields"],
                        tuple(item["clears"]),
                    )
                    pending[notice.marker] = notice
        for notice in notices:
            # A newer transition supersedes undelivered news about the old state.
            clears = set(notice.clears)
            if notice.kind.opens_recovery is not None:
                clears.add(notice.kind.opens_recovery.kind_id)
            pending = {
                key: value for key, value in pending.items() if value.kind.kind_id not in clears
            }
            if notice.kind.scope is not Scope.THRESHOLD:
                pending[notice.marker] = notice
        pending = {
            key: notice
            for key, notice in pending.items()
            if self.notifier.pending(state["username"], notice, seen)
        }

        async def persist() -> None:
            if pending:
                await self.db.set_metadata(
                    storage_key,
                    json.dumps(
                        {
                            "uuid": state["uuid"],
                            "notices": [
                                {**asdict(notice), "kind": notice.kind.name}
                                for notice in pending.values()
                            ],
                        }
                    ),
                )
            else:
                await self.db.delete_metadata(storage_key)

        # Save intent before attempting Telegram; successful markers remain separate.
        await persist()
        mapped = (await self.db.user_tgids()).get(state["username"])
        candidates = (*pending.values(), *(n for n in notices if n.kind.scope is Scope.THRESHOLD))
        for notice in candidates:
            await self.notifier.deliver(
                state["username"],
                int(mapped) if mapped is not None else None,
                notice,
                seen,
            )
            await self.db.sync_notifications(before, seen)
            before = set(seen)
            if not self.notifier.pending(state["username"], notice, seen):
                pending.pop(notice.marker, None)
                await persist()

    async def _quota(self, username: str, *, recover: bool) -> None:
        async def operation() -> None:
            state = await self.sub.user_svc.get_user_state(username)
            now = int(time.time())
            time_ok = state["expires_at"] == 0 or state["expires_at"] > now
            main_ok = state["bw_limit_gb"] == 0 or state["bw_used"] < state["bw_limit_gb"] * 10**9
            wl_ok = state["wl_limit_gb"] == 0 or state["wl_used"] < state["wl_limit_gb"] * 10**9
            # Poll re-enables only; the 120s job handles terminal disables.
            if recover:
                main = (
                    True
                    if time_ok and main_ok and not state["enabled"] and not state["admin_disabled"]
                    else None
                )
                restored_main = main is True
                timing = True if time_ok and not state["enabled_time"] else None
                if timing is not None:
                    restored_main = False
                whitelist = (
                    True
                    if wl_ok and not state["enabled_wl"] and not state["admin_disabled_wl"]
                    else None
                )
                if timing is not None and main is None:
                    main = main_ok and not state["admin_disabled"]
                if main is not None or whitelist is not None:
                    await self.sub.business_svc.update_user(
                        username,
                        enable=main,
                        timee=timing,
                        wl_enable=whitelist,
                        quota=True,
                        work="background",
                    )
                await self._deliver(
                    state,
                    restored(main=restored_main, whitelist=whitelist is True, time=timing is True),
                )
            else:
                notices = classify(state, now)
                terminal = {notice.kind for notice in notices if notice.kind.scope is Scope.EPISODE}
                try:
                    if terminal:
                        disable_main = bool(terminal & {Kind.EXPIRED, Kind.TRAFFIC_EXHAUSTED})
                        # Apply the combined state once: traffic exhaustion must
                        # never overwrite an expired account's time flag.
                        await self.sub.business_svc.update_user(
                            username,
                            enable=False if disable_main else None,
                            timee=time_ok if disable_main else None,
                            wl_enable=False if Kind.WHITELIST_EXHAUSTED in terminal else None,
                            quota=True,
                            work="background",
                        )
                    elif await self.db.get_metadata(f"user_sync_pending:{username}") is not None:
                        # Failed panel writes leave SQLite disabled, so classification
                        # no longer emits a terminal notice. Retry the durable intent
                        # independently of notification eligibility or enabled flags.
                        await self.sub.business_svc.reconcile_user(username)
                except AppError:
                    self.log.exception("background user update failed for %s", username)
                    notices = tuple(notice for notice in notices if notice.kind not in terminal)
                await self._deliver(state, notices)

        try:
            await self.sub.res.coordinator.run([username], operation, name="watcher:quota")
        except AppError:
            self.log.exception("background user update failed for %s", username)

    async def check(self) -> None:
        async def operation() -> None:
            for username in await self.sub.user_svc.list_users():
                await self._quota(username, recover=False)

        await self._job("quota", operation)
