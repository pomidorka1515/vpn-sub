from __future__ import annotations

import asyncio
import json
import time
from bisect import bisect_right
from dataclasses import asdict
from functools import partial
from typing import TYPE_CHECKING

from app_runtime.tasks import TaskRegistry
from errors import AppError
from notify import Notice, classify, restored
from notify.kinds import Kind, Scope

from ._base import WatchJobs

if TYPE_CHECKING:
    from custom_types import UserRecord


class QuotaJobs(WatchJobs):
    """Quota transitions and durable, at-least-once notification delivery.

    State and transition intent commit together. Delivery waits for panel sync;
    interruption after remote acceptance but before marker commit can duplicate
    a send. Newer transitions supersede undelivered news about the old state.
    """

    _NOTICE_WORKERS = 4
    _NOTICE_QUEUE_SIZE = 128
    _NOTICE_RETRY_SECONDS = 15.0

    def _init_notifications(self) -> None:
        self._notice_tasks = TaskRegistry()
        self._notice_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self._NOTICE_QUEUE_SIZE)
        self._notice_enqueued: set[str] = set()
        self._notice_dirty: set[str] = set()
        self._notice_inflight: dict[str, Notice] = {}
        self._notice_cursor: str | None = None
        self._notice_started = False
        self._notice_closed = False

    def _start_notifications(self) -> None:
        if self._notice_started or self._notice_closed or self.notifier is None:
            return
        self._notice_started = True
        for number in range(self._NOTICE_WORKERS):
            self._notice_tasks.create(self._notice_worker, name=f"watcher:notice:{number}")
        self._notice_tasks.create(self._notice_retry_loop, name="watcher:notice-retry")

    def _enqueue_notice(self, username: str) -> None:
        if self._notice_closed or self.notifier is None:
            return
        self._start_notifications()
        if username in self._notice_enqueued:
            self._notice_dirty.add(username)
            return
        try:
            self._notice_queue.put_nowait(username)
        except asyncio.QueueFull:
            # The database is the queue of record; the next scan retries admission.
            return
        self._notice_enqueued.add(username)

    async def _scan_notifications(self) -> None:
        if self.notifier is None or self._notice_closed:
            return
        self._start_notifications()
        prefix = "user_notifications_pending:"
        keys = list(await self.db.list_metadata(prefix))
        start = bisect_right(keys, self._notice_cursor) if self._notice_cursor is not None else 0
        for key in keys[start:] + keys[:start]:
            username = key[len(prefix) :]
            if username in self._notice_enqueued:
                continue
            if self._notice_queue.full():
                break
            self._enqueue_notice(username)
            self._notice_cursor = key

    async def _notice_retry_loop(self) -> None:
        while True:
            await asyncio.sleep(self._NOTICE_RETRY_SECONDS)
            try:
                await self._scan_notifications()
            except Exception:
                self.log.exception("notification retry scan failed")

    async def _notice_worker(self) -> None:
        while True:
            username = await self._notice_queue.get()
            try:
                await self._process_notices(username)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                self.log.warning("notification attempt cancelled for %s", username)
            except Exception:
                self.log.exception("notification delivery failed for %s", username)
            finally:
                self._notice_inflight.pop(username, None)
                self._notice_enqueued.discard(username)
                if username in self._notice_dirty:
                    self._notice_dirty.discard(username)
                    self._enqueue_notice(username)
                self._notice_queue.task_done()

    async def drain_notifications(self) -> None:
        """Wait for currently admitted attempts, not future retries."""
        await self._notice_queue.join()

    async def _close_notifications(self) -> None:
        self._notice_closed = True
        await self._notice_tasks.drain(cancel=True)

    async def _pending_notices(
        self, state: UserRecord, notices: tuple[Notice, ...], seen: set[str]
    ) -> dict[str, Notice]:
        storage_key = f"user_notifications_pending:{state['username']}"
        raw = await self.db.get_metadata(storage_key)
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
            pending[notice.marker] = notice
        inflight = self._notice_inflight.get(state["username"])
        return {
            key: notice
            for key, notice in pending.items()
            if self.notifier is not None
            and (
                self.notifier.pending(state["username"], notice, seen)
                or (
                    notice.kind.scope is Scope.ONCE
                    and inflight is not None
                    and inflight.kind.opens_recovery is notice.kind
                )
            )
        }

    @staticmethod
    def _serialize_intent(state: UserRecord, pending: dict[str, Notice]) -> str:
        return json.dumps(
            {
                "uuid": state["uuid"],
                "notices": [
                    {**asdict(notice), "kind": notice.kind.name} for notice in pending.values()
                ],
            }
        )

    async def _transition_intent(
        self, state: UserRecord, notices: tuple[Notice, ...]
    ) -> str | None:
        if self.notifier is None:
            return None
        seen = await self.db.notification_markers()
        return self._serialize_intent(state, await self._pending_notices(state, notices, seen))

    async def _deliver(self, state: UserRecord, notices: tuple[Notice, ...]) -> None:
        if self.notifier is None:
            return
        storage_key = f"user_notifications_pending:{state['username']}"
        if not notices and await self.db.get_metadata(storage_key) is None:
            return
        seen = await self.db.notification_markers()
        pending = await self._pending_notices(state, notices, seen)
        await self._save_notices(state, pending)
        if pending:
            self._enqueue_notice(state["username"])

    async def _save_notices(self, state: UserRecord, pending: dict[str, Notice]) -> None:
        key = f"user_notifications_pending:{state['username']}"
        if pending:
            await self.db.set_metadata(key, self._serialize_intent(state, pending))
        else:
            await self.db.delete_metadata(key)

    @staticmethod
    def _notice_current(state: UserRecord, notice: Notice) -> bool:
        now = int(time.time())
        if notice.kind.scope is Scope.THRESHOLD:
            return any(item.marker == notice.marker for item in classify(state, now))
        if notice.kind is Kind.EXPIRED:
            return state["expires_at"] != 0 and state["expires_at"] <= now
        if notice.kind is Kind.TRAFFIC_EXHAUSTED:
            return state["bw_limit_gb"] != 0 and state["bw_used"] >= state["bw_limit_gb"] * 10**9
        if notice.kind is Kind.WHITELIST_EXHAUSTED:
            return state["wl_limit_gb"] != 0 and state["wl_used"] >= state["wl_limit_gb"] * 10**9
        if notice.kind is Kind.WHITELIST_RESTORED:
            return bool(state["enabled_wl"])
        return ("traffic_exhausted" not in notice.clears or bool(state["enabled"])) and (
            "expired" not in notice.clears or bool(state["enabled_time"])
        )

    async def _prepare_notice(
        self, username: str, attempted: set[str]
    ) -> tuple[str, int, Notice, set[str]] | None:
        async def operation() -> tuple[str, int, Notice, set[str]] | None:
            state = await self.db.get_user(username)
            if (
                state is None
                or await self.db.get_metadata(f"user_sync_pending:{username}") is not None
            ):
                return None
            seen = await self.db.notification_markers()
            pending = await self._pending_notices(state, (), seen)
            valid = {
                key: notice
                for key, notice in pending.items()
                if self._notice_current(state, notice)
            }
            if valid != pending:
                await self._save_notices(state, valid)
            if not valid:
                # Also remove a stale UUID or already-acknowledged outbox.
                await self._save_notices(state, {})
                return None
            mapped = (await self.db.user_tgids()).get(username)
            if mapped is None:
                return None
            for marker, notice in valid.items():
                if marker not in attempted:
                    self._notice_inflight[username] = notice
                    return state["uuid"], int(mapped), notice, seen
            return None

        return await self.sub.res.coordinator.run(
            [username], operation, name="watcher:notice-prepare"
        )

    async def _validate_notice(self, username: str, uuid: str, tgid: int, notice: Notice) -> bool:
        async def operation() -> bool:
            state = await self.db.get_user(username)
            if state is None or state["uuid"] != uuid:
                return False
            if await self.db.get_metadata(f"user_sync_pending:{username}") is not None:
                return False
            seen = await self.db.notification_markers()
            pending = await self._pending_notices(state, (), seen)
            mapped = (await self.db.user_tgids()).get(username)
            return (
                mapped is not None
                and int(mapped) == tgid
                and pending.get(notice.marker) == notice
                and self._notice_current(state, notice)
            )

        return await self.sub.res.coordinator.run(
            [username], operation, name="watcher:notice-validate"
        )

    async def _ack_notice(
        self, username: str, uuid: str, tgid: int, notice: Notice, before: set[str], after: set[str]
    ) -> None:
        async def operation() -> None:
            state = await self.db.get_user(username)
            if state is None or state["uuid"] != uuid:
                return
            mapped = (await self.db.user_tgids()).get(username)
            if mapped is None or int(mapped) != tgid:
                return
            current = await self.db.notification_markers()
            pending = await self._pending_notices(state, (), current)
            recovery = notice.kind.opens_recovery
            superseded_by_recovery = recovery is not None and any(
                item.kind is recovery and notice.kind.kind_id in item.clears
                for item in pending.values()
            )
            if pending.get(notice.marker) != notice and not superseded_by_recovery:
                return
            if not superseded_by_recovery and not self._notice_current(state, notice):
                return
            updated = (current - (before - after)) | (after - before)
            await self.db.sync_notifications(current, updated)
            pending.pop(notice.marker, None)
            await self._save_notices(state, pending)

        await self.sub.res.coordinator.run([username], operation, name="watcher:notice-ack")

    async def _process_notices(self, username: str) -> None:
        if self.notifier is None:
            return
        attempted: set[str] = set()
        while prepared := await self._prepare_notice(username, attempted):
            uuid, tgid, notice, seen = prepared
            attempted.add(notice.marker)
            before = set(seen)
            try:
                accepted = await self.notifier.deliver(
                    username,
                    tgid,
                    notice,
                    seen,
                    validate=partial(self._validate_notice, username, uuid, tgid, notice),
                )
                if accepted:
                    await self._ack_notice(username, uuid, tgid, notice, before, seen)
            finally:
                self._notice_inflight.pop(username, None)

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
                notices = restored(
                    main=restored_main, whitelist=whitelist is True, time=timing is True
                )
                if main is not None or whitelist is not None:
                    await self.sub.business_svc.update_user(
                        username,
                        enable=main,
                        timee=timing,
                        wl_enable=whitelist,
                        quota=True,
                        work="background",
                        notification_intent=await self._transition_intent(state, notices),
                    )
                elif await self.db.get_metadata(f"user_sync_pending:{username}") is not None:
                    await self.sub.business_svc.reconcile_user(username)
                await self._deliver(state, notices)
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
                            notification_intent=await self._transition_intent(state, notices),
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
