"""SQLite persistence for dynamic subscription state.

The JSON configuration contains deployment and presentation settings. This
package owns mutable user, code, quota, Telegram, notification and snapshot
state. ``Database`` owns a task-safe writer and leased read connections.
All repository I/O is awaited and resources have explicit lifecycles.
"""

from __future__ import annotations

from errors import CodeError, DatabaseError, DuplicateError, MigrationError

from .database import Database

__all__ = [
    "CodeError",
    "Database",
    "DatabaseError",
    "DuplicateError",
    "MigrationError",
]
