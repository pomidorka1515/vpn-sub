from __future__ import annotations

from typing import TYPE_CHECKING

from .async_common import AsyncConnectionMixin

if TYPE_CHECKING:
    from .async_common import AsyncConnection


async def set_telegram_mapping(
    conn: AsyncConnection, username: str, telegram_id: str | None
) -> None:
    if telegram_id is None:
        await conn.execute("DELETE FROM telegram_mappings WHERE username = ?", (username,))
        return
    await conn.execute(
        "DELETE FROM telegram_mappings WHERE telegram_id = ? OR username = ?",
        (telegram_id, username),
    )
    await conn.execute(
        "INSERT INTO telegram_mappings(telegram_id, username) VALUES (?, ?)",
        (telegram_id, username),
    )


class AsyncTelegramMixin(AsyncConnectionMixin):
    async def set_telegram(self, username: str, telegram_id: str | None) -> None:
        async with self.transaction(immediate=True) as conn:
            await set_telegram_mapping(conn, username, telegram_id)

    async def tgid_to_user(self, tgid: str | int) -> str | None:
        async with self.connection() as conn:
            row = await conn.row(
                "SELECT username FROM telegram_mappings WHERE telegram_id = ?", (str(tgid),)
            )
            return str(row[0]) if row else None

    async def user_to_tgid(self, username: str) -> str | None:
        async with self.connection() as conn:
            row = await conn.row(
                "SELECT telegram_id FROM telegram_mappings WHERE username = ?", (username,)
            )
            return str(row[0]) if row else None

    async def user_tgids(self) -> dict[str, str]:
        async with self.connection() as conn:
            return {
                str(row[0]): str(row[1])
                for row in await conn.rows("SELECT username, telegram_id FROM telegram_mappings")
            }

    async def get_telegram_language(self, telegram_id: str | int, default: str = "ru") -> str:
        async with self.connection() as conn:
            row = await conn.row(
                "SELECT language FROM telegram_preferences WHERE telegram_id = ?",
                (str(telegram_id),),
            )
            return str(row[0]) if row else default

    async def has_telegram_language(self, telegram_id: str | int) -> bool:
        async with self.connection() as conn:
            return (
                await conn.row(
                    "SELECT 1 FROM telegram_preferences WHERE telegram_id = ?", (str(telegram_id),)
                )
                is not None
            )

    async def set_telegram_language(self, telegram_id: str | int, language: str) -> None:
        async with self.transaction(immediate=True) as conn:
            await conn.execute(
                "INSERT INTO telegram_preferences(telegram_id, language) VALUES (?, ?) ON CONFLICT(telegram_id) DO UPDATE SET language=excluded.language",
                (str(telegram_id), language),
            )
