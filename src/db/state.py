from __future__ import annotations

import json
from typing import TYPE_CHECKING, TypedDict, cast

from custom_types import BandwidthSnapshotPayload

from .common import ConnectionMixin

if TYPE_CHECKING:
    from collections.abc import Mapping

    from config.constants import JsonDict


class StateSnapshot(TypedDict):
    ts: int
    host: JsonDict
    panels: dict[str, JsonDict]


class StateMixin(ConnectionMixin):
    def increment_usage(self, username: str, *, regular: int = 0, whitelist: int = 0) -> None:
        with self.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE users SET bw_used = bw_used + ?, wl_used = wl_used + ? WHERE username = ?",
                (regular, whitelist, username),
            )

    def increment_usages(self, updates: Mapping[str, tuple[int, int]]) -> None:
        """Apply (regular, whitelist) deltas in one transaction.

        Empty input is a no-op and does not open a write transaction.
        """
        rows = [
            (regular, whitelist, username)
            for username, (regular, whitelist) in updates.items()
            if regular or whitelist
        ]
        if not rows:
            return
        with self.transaction(immediate=True) as conn:
            conn.executemany(
                "UPDATE users SET bw_used = bw_used + ?, wl_used = wl_used + ? WHERE username = ?",
                rows,
            )

    def reset_monthly(self, month: str, today: str) -> bool:
        with self.transaction(immediate=True) as conn:
            current = conn.execute(
                "SELECT value FROM app_metadata WHERE key = 'last_reset_month'"
            ).fetchone()
            if current is not None and current[0] == month:
                return False
            conn.execute("UPDATE users SET bw_used = 0, wl_used = 0")
            for key, value in (("last_reset", today), ("last_reset_month", month)):
                conn.execute(
                    "INSERT INTO app_metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
            conn.execute("DELETE FROM notification_state")
            return True

    def clear_notifications(self) -> None:
        with self.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM notification_state")

    def notification_markers(self) -> set[str]:
        """Every stored marker. One read for a quota cycle.

        Legacy rows keyed by Telegram id are not markers. A monthly reset
        deletes those; the quota diff must not treat them as live state.
        """
        with self.connection() as conn:
            return {
                str(row[0])
                for row in conn.execute(
                    "SELECT kind FROM notification_state WHERE telegram_id = ''"
                )
            }

    def sync_notifications(self, before: set[str], after: set[str]) -> None:
        """Apply the marker diff from one quota cycle.

        Markers this cycle did not load are left alone. A failed send is
        absent from ``after`` and is therefore not inserted.
        Both columns identify a row: deleting by ``kind`` alone would also
        remove a legacy Telegram-id row that happens to share the kind.
        """
        added = after - before
        removed = before - after
        if not added and not removed:
            return
        with self.transaction(immediate=True) as conn:
            if removed:
                conn.executemany(
                    "DELETE FROM notification_state WHERE kind = ? AND telegram_id = ''",
                    [(kind,) for kind in sorted(removed)],
                )
            if added:
                conn.executemany(
                    "INSERT OR IGNORE INTO notification_state(kind, telegram_id) VALUES (?, '')",
                    [(kind,) for kind in sorted(added)],
                )

    def notification_seen(self, kind: str, telegram_id: int | str) -> bool:
        with self.connection() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM notification_state WHERE kind = ? AND telegram_id = ?",
                    (kind, str(telegram_id)),
                ).fetchone()
                is not None
            )

    def mark_notification(self, kind: str, telegram_id: int | str) -> bool:
        with self.transaction(immediate=True) as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO notification_state(kind, telegram_id) VALUES (?, ?)",
                (kind, str(telegram_id)),
            )
            return cur.rowcount == 1

    def get_metadata(self, key: str, default: str | None = None) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM app_metadata WHERE key = ?", (key,)).fetchone()
            return str(row[0]) if row else default

    def set_metadata(self, key: str, value: str) -> None:
        with self.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO app_metadata(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def list_metadata(self, prefix: str) -> dict[str, str]:
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT key, value FROM app_metadata WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
                (f"{escaped}%",),
            )
            return {str(row["key"]): str(row["value"]) for row in rows}

    def delete_metadata(self, key: str) -> None:
        with self.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM app_metadata WHERE key = ?", (key,))

    def admin_ui_session(self) -> str | None:
        value = self.get_metadata("admin_ui_session")
        return value or None

    def set_admin_ui_session(self, token: str | None) -> None:
        self.set_metadata("admin_ui_session", token or "")

    def add_bandwidth_snapshot(
        self, username: str, ts: int, up: int, down: int, wl_up: int, wl_down: int
    ) -> None:
        with self.transaction(immediate=True) as conn:
            conn.execute(
                """INSERT INTO bandwidth_snapshots(username, ts, up, down, wl_up, wl_down)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(username, ts) DO UPDATE SET
                up=up + excluded.up, down=down + excluded.down,
                wl_up=wl_up + excluded.wl_up, wl_down=wl_down + excluded.wl_down""",
                (username, ts, up, down, wl_up, wl_down),
            )

    def add_bandwidth_snapshots(
        self,
        rows: list[tuple[str, int, int, int, int, int]],
    ) -> None:
        """Upsert many daily snapshots in one transaction.

        Each row is (username, ts, up, down, wl_up, wl_down). Empty input is
        a no-op and does not open a write transaction.
        """
        if not rows:
            return
        with self.transaction(immediate=True) as conn:
            conn.executemany(
                """INSERT INTO bandwidth_snapshots(username, ts, up, down, wl_up, wl_down)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(username, ts) DO UPDATE SET
                up=up + excluded.up, down=down + excluded.down,
                wl_up=wl_up + excluded.wl_up, wl_down=wl_down + excluded.wl_down""",
                rows,
            )

    def get_bandwidth_snapshots(self, username: str, cutoff: int) -> list[BandwidthSnapshotPayload]:
        with self.connection() as conn:
            return [
                BandwidthSnapshotPayload(
                    ts=int(row["ts"]),
                    up=int(row["up"]),
                    down=int(row["down"]),
                    wl_up=int(row["wl_up"]),
                    wl_down=int(row["wl_down"]),
                )
                for row in conn.execute(
                    "SELECT ts, up, down, wl_up, wl_down FROM bandwidth_snapshots WHERE username = ? AND ts >= ? ORDER BY ts DESC",
                    (username, cutoff),
                )
            ]

    def prune_bandwidth_snapshots(self, cutoff: int) -> int:
        with self.transaction(immediate=True) as conn:
            cur = conn.execute("DELETE FROM bandwidth_snapshots WHERE ts < ?", (cutoff,))
            return cur.rowcount

    def upsert_state_snapshot(self, ts: int, payload: StateSnapshot) -> None:
        with self.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO state_snapshots(ts, payload) VALUES (?, ?) ON CONFLICT(ts) DO UPDATE SET payload=excluded.payload",
                (ts, json.dumps(payload, separators=(",", ":"))),
            )

    def get_state_snapshots(self, cutoff: int) -> list[StateSnapshot]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT payload FROM state_snapshots WHERE ts >= ? ORDER BY ts DESC", (cutoff,)
            ).fetchall()
        return [cast(StateSnapshot, json.loads(str(row[0]))) for row in rows]

    def prune_state_snapshots(self, cutoff: int) -> int:
        with self.transaction(immediate=True) as conn:
            cur = conn.execute("DELETE FROM state_snapshots WHERE ts < ?", (cutoff,))
            return cur.rowcount
