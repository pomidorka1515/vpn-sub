from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3


def drop_usage_cache_schema(conn: sqlite3.Connection) -> None:
    """Remove v10 objects before reconstructing a legacy test database."""
    conn.executescript(
        """DROP TRIGGER usage_dirty_insert;
        DROP TRIGGER usage_dirty_delete;
        DROP TRIGGER usage_dirty_update;
        DROP TABLE usage_dirty;
        DROP INDEX idx_users_usage_main_recovery;
        DROP INDEX idx_users_usage_wl_recovery;"""
    )
