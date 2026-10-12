from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from custom_types import BandwidthSnapshotPayload

from .async_common import AsyncConnectionMixin
from .state import StateSnapshot

if TYPE_CHECKING:
    from collections.abc import Mapping


class AsyncStateMixin(AsyncConnectionMixin):
    async def increment_usage(self, username: str, *, regular: int = 0, whitelist: int = 0) -> None:
        await self.increment_usages({username: (regular, whitelist)})

    async def increment_usages(self, updates: Mapping[str, tuple[int, int]]) -> None:
        rows = [
            (regular, whitelist, username)
            for username, (regular, whitelist) in updates.items()
            if regular or whitelist
        ]
        if rows:
            async with self.transaction(immediate=True) as conn:
                await conn.executemany(
                    "UPDATE users SET bw_used = bw_used + ?, wl_used = wl_used + ? WHERE username = ?",
                    rows,
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
            await conn.execute("DELETE FROM notification_state")
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
        return await self.get_metadata("admin_ui_session") or None

    async def set_admin_ui_session(self, token: str | None) -> None:
        await self.set_metadata("admin_ui_session", token or "")

    async def add_bandwidth_snapshot(
        self, username: str, ts: int, up: int, down: int, wl_up: int, wl_down: int,
    ) -> None:
        await self.add_bandwidth_snapshots([(username, ts, up, down, wl_up, wl_down)])

    async def add_bandwidth_snapshots(
        self, rows: list[tuple[str, int, int, int, int, int]],
    ) -> None:
        if rows:
            async with self.transaction(immediate=True) as conn:
                await conn.executemany(
                    """INSERT INTO bandwidth_snapshots(username, ts, up, down, wl_up, wl_down)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(username, ts) DO UPDATE SET
                up=up + excluded.up, down=down + excluded.down,
                wl_up=wl_up + excluded.wl_up, wl_down=wl_down + excluded.wl_down""",
                    rows,
                )

    async def get_bandwidth_snapshots(
        self, username: str, cutoff: int,
    ) -> list[BandwidthSnapshotPayload]:
        async with self.connection() as conn:
            return [
                BandwidthSnapshotPayload(
                    ts=int(row["ts"]),
                    up=int(row["up"]),
                    down=int(row["down"]),
                    wl_up=int(row["wl_up"]),
                    wl_down=int(row["wl_down"]),
                )
                for row in await conn.rows(
                    "SELECT ts, up, down, wl_up, wl_down FROM bandwidth_snapshots WHERE username = ? AND ts >= ? ORDER BY ts DESC",
                    (username, cutoff),
                )
            ]

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
                "SELECT payload FROM state_snapshots WHERE ts >= ? ORDER BY ts DESC", (cutoff,),
            )
        return [cast(StateSnapshot, json.loads(str(row[0]))) for row in rows]

    async def prune_state_snapshots(self, cutoff: int) -> int:
        async with self.transaction(immediate=True) as conn:
            return await conn.execute("DELETE FROM state_snapshots WHERE ts < ?", (cutoff,))
