from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, TypedDict, cast

from constants import SESSION_MAX_AGE
from custom_types import BandwidthSnapshotPayload
from notify import Kind, Scope, split_key

from .common import ConnectionMixin
from .notification_episodes import clear_notification_episodes

if TYPE_CHECKING:
    from collections.abc import Mapping

    from config.constants import JsonDict

    from .common import Connection


class StateSnapshot(TypedDict):
    ts: int
    panels: dict[str, JsonDict]
    host: JsonDict


class StateMixin(ConnectionMixin):
    @staticmethod
    async def _reset_monthly_notifications(conn: Connection) -> None:
        # Monthly buckets expire; delivered outages and recovery acknowledgements
        # belong to episodes, which can outlive the accounting period.
        thresholds = {kind.kind_id for kind in Kind if kind.scope is Scope.THRESHOLD}
        for row in await conn.rows("SELECT kind FROM notification_state WHERE telegram_id = ''"):
            parsed = split_key(str(row[0]))
            if parsed is not None and parsed[1].partition(":")[0] in thresholds:
                await conn.execute(
                    "DELETE FROM notification_state WHERE kind = ? AND telegram_id = ''", (row[0],)
                )
        # Reset counters make unsent exhaustion news obsolete, even if cancellation
        # lets the quota job run before recovery. Keep expiry and recovery intent;
        # recovery will atomically replace intent with the re-enable transition.
        obsolete = {
            Kind.TRAFFIC_EXHAUSTED.name,
            Kind.WHITELIST_EXHAUSTED.name,
            Kind.TRAFFIC_SOON.name,
            Kind.WHITELIST_SOON.name,
        }
        for row in await conn.rows(
            "SELECT key, value FROM app_metadata WHERE key GLOB 'user_notifications_pending:*'"
        ):
            saved = json.loads(str(row[1]))
            notices = saved["notices"]
            saved["notices"] = [item for item in notices if item["kind"] not in obsolete]
            if saved["notices"] == notices:
                continue
            if saved["notices"]:
                await conn.execute(
                    "UPDATE app_metadata SET value = ? WHERE key = ?", (json.dumps(saved), row[0])
                )
            else:
                await conn.execute("DELETE FROM app_metadata WHERE key = ?", (row[0],))

    async def increment_usage(self, username: str, *, regular: int = 0, whitelist: int = 0) -> None:
        await self.increment_usages({username: (regular, whitelist)})

    async def increment_usages(
        self,
        updates: Mapping[str, tuple[int, int]],
        *,
        baselines: str | None = None,
        period: tuple[str, str] | None = None,
    ) -> None:
        rows = [
            (regular, whitelist, username)
            for username, (regular, whitelist) in updates.items()
            if regular or whitelist
        ]
        if rows or baselines is not None or period is not None:
            async with self.transaction(immediate=True) as conn:
                if period is not None:
                    month, today = period
                    row = await conn.row(
                        "SELECT value FROM app_metadata WHERE key = 'last_reset_month'"
                    )
                    if row is None or row[0] != month:
                        # On upgrade, preserve usage when no period was recorded.
                        if row is not None:
                            await conn.execute("UPDATE users SET bw_used = 0, wl_used = 0")
                            await self._reset_monthly_notifications(conn)
                        await conn.executemany(
                            "INSERT INTO app_metadata(key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            [("last_reset", today), ("last_reset_month", month)],
                        )
                await conn.executemany(
                    "UPDATE users SET bw_used = bw_used + ?, wl_used = wl_used + ? WHERE username = ?",
                    rows,
                )
                # Normal positive usage must not reopen warning buckets, but a
                # correction that decreases usage starts a new threshold episode.
                for regular, whitelist, username in rows:
                    if regular >= 0 and whitelist >= 0:
                        continue
                    sides = {
                        side
                        for side, delta in (("traffic", regular), ("whitelist", whitelist))
                        if delta < 0
                    }
                    if await conn.row("SELECT 1 FROM users WHERE username = ?", (username,)):
                        await clear_notification_episodes(conn, username, sides=sides)
                if baselines is not None:
                    await conn.execute(
                        "INSERT INTO app_metadata(key, value) VALUES ('watcher_usage_baselines', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (baselines,),
                    )

    async def reset_monthly(self, month: str, today: str) -> bool:
        async with self.transaction(immediate=True) as conn:
            row = await conn.row("SELECT value FROM app_metadata WHERE key = 'last_reset_month'")
            if row is not None and row[0] == month:
                return False
            await conn.execute("UPDATE users SET bw_used = 0, wl_used = 0")
            await conn.executemany(
                "INSERT INTO app_metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [("last_reset", today), ("last_reset_month", month)],
            )
            await self._reset_monthly_notifications(conn)
            return True

    async def clear_notifications(self) -> None:
        async with self.transaction(immediate=True) as conn:
            await conn.execute("DELETE FROM notification_state")

    async def notification_markers(self) -> set[str]:
        async with self.connection() as conn:
            return {
                str(row[0])
                for row in await conn.rows(
                    "SELECT kind FROM notification_state WHERE telegram_id = ''",
                )
            }

    async def sync_notifications(self, before: set[str], after: set[str]) -> None:
        added, removed = after - before, before - after
        if not added and not removed:
            return
        async with self.transaction(immediate=True) as conn:
            if removed:
                await conn.executemany(
                    "DELETE FROM notification_state WHERE kind = ? AND telegram_id = ''",
                    [(kind,) for kind in sorted(removed)],
                )
            if added:
                await conn.executemany(
                    "INSERT OR IGNORE INTO notification_state(kind, telegram_id) VALUES (?, '')",
                    [(kind,) for kind in sorted(added)],
                )

    async def notification_seen(self, kind: str, telegram_id: int | str) -> bool:
        async with self.connection() as conn:
            return (
                await conn.row(
                    "SELECT 1 FROM notification_state WHERE kind = ? AND telegram_id = ?",
                    (kind, str(telegram_id)),
                )
                is not None
            )

    async def mark_notification(self, kind: str, telegram_id: int | str) -> bool:
        async with self.transaction(immediate=True) as conn:
            return (
                await conn.execute(
                    "INSERT OR IGNORE INTO notification_state(kind, telegram_id) VALUES (?, ?)",
                    (kind, str(telegram_id)),
                )
                == 1
            )

    async def get_metadata(self, key: str, default: str | None = None) -> str | None:
        async with self.connection() as conn:
            row = await conn.row("SELECT value FROM app_metadata WHERE key = ?", (key,))
            return str(row[0]) if row else default

    async def set_metadata(self, key: str, value: str) -> None:
        async with self.transaction(immediate=True) as conn:
            await conn.execute(
                "INSERT INTO app_metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    async def list_metadata(self, prefix: str) -> dict[str, str]:
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        async with self.connection() as conn:
            return {
                str(row["key"]): str(row["value"])
                for row in await conn.rows(
                    "SELECT key, value FROM app_metadata WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
                    (f"{escaped}%",),
                )
            }

    async def delete_metadata(self, key: str) -> None:
        async with self.transaction(immediate=True) as conn:
            await conn.execute("DELETE FROM app_metadata WHERE key = ?", (key,))

    async def admin_ui_session(self) -> str | None:
        async with self.connection() as conn:
            rows = await conn.rows(
                "SELECT key, value FROM app_metadata WHERE key IN ('admin_ui_session', 'admin_ui_session_expires_at')",
            )
        values = {str(row[0]): str(row[1]) for row in rows}
        try:
            expires_at = int(values.get("admin_ui_session_expires_at", "0"))
        except ValueError:
            return None
        if expires_at <= time.time():
            return None
        return values.get("admin_ui_session") or None

    async def set_admin_ui_session(self, token: str | None) -> None:
        async with self.transaction(immediate=True) as conn:
            await conn.executemany(
                "INSERT INTO app_metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [
                    ("admin_ui_session", token or ""),
                    (
                        "admin_ui_session_expires_at",
                        str(int(time.time()) + SESSION_MAX_AGE) if token else "0",
                    ),
                ],
            )

    async def add_bandwidth_snapshot(
        self,
        username: str,
        ts: int,
        up: int,
        down: int,
        wl_up: int,
        wl_down: int,
    ) -> None:
        await self.add_bandwidth_snapshots([(username, ts, up, down, wl_up, wl_down)])

    async def add_bandwidth_snapshots(
        self,
        rows: list[tuple[str, int, int, int, int, int]],
        *,
        baselines: str | None = None,
        starts: dict[str, int | None] | None = None,
    ) -> None:
        if rows or baselines is not None:
            async with self.transaction(immediate=True) as conn:
                await conn.executemany(
                    """INSERT INTO bandwidth_snapshots(username, ts, up, down, wl_up, wl_down, start_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(username, ts) DO UPDATE SET
                up=up + excluded.up, down=down + excluded.down,
                wl_up=wl_up + excluded.wl_up, wl_down=wl_down + excluded.wl_down,
                start_ts=CASE WHEN start_ts IS NOT NULL AND excluded.start_ts IS NOT NULL
                    THEN min(start_ts, excluded.start_ts) ELSE NULL END""",
                    [(*row, (starts or {}).get(row[0])) for row in rows],
                )
                if baselines is not None:
                    await conn.execute(
                        "INSERT INTO app_metadata(key, value) VALUES ('watcher_snapshot_baselines', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (baselines,),
                    )

    async def get_bandwidth_snapshots(
        self,
        username: str,
        cutoff: int,
    ) -> list[BandwidthSnapshotPayload]:
        async with self.connection() as conn:
            result: list[BandwidthSnapshotPayload] = []
            for row in await conn.rows(
                "SELECT ts, start_ts, up, down, wl_up, wl_down FROM bandwidth_snapshots WHERE username = ? AND ts >= ? ORDER BY ts DESC",
                (username, cutoff),
            ):
                payload = BandwidthSnapshotPayload(
                    ts=int(row["ts"]),
                    up=int(row["up"]),
                    down=int(row["down"]),
                    wl_up=int(row["wl_up"]),
                    wl_down=int(row["wl_down"]),
                )
                if row["start_ts"] is not None:
                    payload["start_ts"] = int(row["start_ts"])
                result.append(payload)
            return result

    async def prune_bandwidth_snapshots(self, cutoff: int) -> int:
        async with self.transaction(immediate=True) as conn:
            return await conn.execute("DELETE FROM bandwidth_snapshots WHERE ts < ?", (cutoff,))

    async def upsert_state_snapshot(self, ts: int, payload: StateSnapshot) -> None:
        async with self.transaction(immediate=True) as conn:
            await conn.execute(
                "INSERT INTO state_snapshots(ts, payload) VALUES (?, ?) ON CONFLICT(ts) DO UPDATE SET payload=excluded.payload",
                (ts, json.dumps(payload, separators=(",", ":"))),
            )

    async def get_state_snapshots(self, cutoff: int) -> list[StateSnapshot]:
        async with self.connection() as conn:
            rows = await conn.rows(
                "SELECT payload FROM state_snapshots WHERE ts >= ? ORDER BY ts DESC",
                (cutoff,),
            )
        return [cast(StateSnapshot, json.loads(str(row[0]))) for row in rows]

    async def prune_state_snapshots(self, cutoff: int) -> int:
        async with self.transaction(immediate=True) as conn:
            return await conn.execute("DELETE FROM state_snapshots WHERE ts < ?", (cutoff,))
