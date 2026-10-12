"""SQLite persistence for dynamic subscription state.

The JSON configuration contains deployment and presentation settings. This
package owns mutable user, code, quota, Telegram, notification and snapshot
state. ``AsyncDatabase`` owns a task-safe writer and leased read connections;
``Database`` retains the synchronous interface for staged callers.
"""

from __future__ import annotations

from errors import CodeError, DatabaseError, DuplicateError, MigrationError

from .async_database import AsyncDatabase
from .database import Database

__all__ = [
    "AsyncDatabase",
    "CodeError",
    "Database",
    "DatabaseError",
    "DuplicateError",
    "MigrationError",
]
