from __future__ import annotations

import sqlite3

from .common import ConnectionMixin


def set_telegram_mapping(
    conn: sqlite3.Connection,
    username: str,
    telegram_id: str | None,
) -> None:
    if telegram_id is None:
        conn.execute("DELETE FROM telegram_mappings WHERE username = ?", (username,))
        return
    # a login reassigns both sides of the one-to-one mapping
    # matching the old table's last-login-wins behavior without stale reverse rows
    conn.execute(
        "DELETE FROM telegram_mappings WHERE telegram_id = ? OR username = ?",
        (telegram_id, username),
    )
    conn.execute(
        "INSERT INTO telegram_mappings(telegram_id, username) VALUES (?, ?)",
        (telegram_id, username),
    )


class TelegramMixin(ConnectionMixin):
    def set_telegram(self, username: str, telegram_id: str | None) -> None:
        with self.transaction(immediate=True) as conn:
            set_telegram_mapping(conn, username, telegram_id)

    def tgid_to_user(self, tgid: str | int) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT username FROM telegram_mappings WHERE telegram_id = ?", (str(tgid),)).fetchone()
            return str(row[0]) if row else None

    def user_to_tgid(self, username: str) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT telegram_id FROM telegram_mappings WHERE username = ?", (username,)).fetchone()
            return str(row[0]) if row else None

    def get_telegram_language(self, telegram_id: str | int, default: str = "ru") -> str:
        with self.connection() as conn:
            row = conn.execute("SELECT language FROM telegram_preferences WHERE telegram_id = ?", (str(telegram_id),)).fetchone()
            return str(row[0]) if row else default

    def has_telegram_language(self, telegram_id: str | int) -> bool:
        with self.connection() as conn:
            return conn.execute("SELECT 1 FROM telegram_preferences WHERE telegram_id = ?", (str(telegram_id),)).fetchone() is not None

    def set_telegram_language(self, telegram_id: str | int, language: str) -> None:
        with self.transaction(immediate=True) as conn:
            conn.execute("INSERT INTO telegram_preferences(telegram_id, language) VALUES (?, ?) ON CONFLICT(telegram_id) DO UPDATE SET language=excluded.language", (str(telegram_id), language))
