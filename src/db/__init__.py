"""SQLite persistence for dynamic subscription state.

The JSON configuration contains deployment and presentation settings. This
package owns mutable user, code, quota, Telegram, notification and snapshot
state. Connections are deliberately short lived: every operation obtains a
connection for its transaction, which is safe for Flask and background threads.
"""

from __future__ import annotations

from errors import CodeError, DatabaseError, DuplicateError, MigrationError

from .database import Database

__all__ = [
    "Database",
    "DatabaseError",
    "DuplicateError",
    "CodeError",
    "MigrationError",
]
