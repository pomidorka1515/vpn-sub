from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .common import Connection


async def clear_notification_episodes(
    conn: Connection,
    username: str,
    *,
    sides: set[str],
    identity: bool = False,
    new_uuid: str | None = None,
) -> None:
    """Forget changed warning buckets, but retain delivered outage evidence."""
    thresholds = {f"{side}_soon" for side in sides}
    prefix = f"{username}:"
    rows = await conn.rows(
        "SELECT kind FROM notification_state WHERE telegram_id = '' AND substr(kind, 1, ?) = ?",
        (len(prefix), prefix),
    )
    for marker in rows:
        kind = str(marker[0])[len(prefix) :].partition(":")[0]
        if identity or kind in thresholds:
            await conn.execute(
                "DELETE FROM notification_state WHERE kind = ? AND telegram_id = ''", (marker[0],)
            )
    key = f"user_notifications_pending:{username}"
    row = await conn.row("SELECT value FROM app_metadata WHERE key = ?", (key,))
    if row is None:
        return
    if identity:
        await conn.execute("DELETE FROM app_metadata WHERE key = ?", (key,))
        return
    obsolete = {
        "traffic": {"TRAFFIC_SOON", "TRAFFIC_EXHAUSTED"},
        "whitelist": {"WHITELIST_SOON", "WHITELIST_EXHAUSTED"},
        "expiry": {"EXPIRY_SOON", "EXPIRED"},
    }
    excluded = {kind for side in sides for kind in obsolete[side]}
    saved = json.loads(str(row[0]))
    saved["notices"] = [item for item in saved["notices"] if item["kind"] not in excluded]
    if new_uuid is not None:
        saved["uuid"] = new_uuid
    if saved["notices"]:
        await conn.execute(
            "UPDATE app_metadata SET value = ? WHERE key = ?", (json.dumps(saved), key)
        )
    else:
        await conn.execute("DELETE FROM app_metadata WHERE key = ?", (key,))
