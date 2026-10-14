from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from app_runtime.tasks import TaskRegistry

if TYPE_CHECKING:
    import asyncio
    from pathlib import Path

    from db import Database

__all__ = ["DatabaseSessionStore", "SessionRecord", "SessionStore"]


@dataclass(frozen=True, slots=True)
class SessionRecord:
    user_id: str
    token: str
    lang: str | None

    @property
    def logged_in(self) -> bool:
        return bool(self.token)


class SessionStore(Protocol):
    """Immediate session access used by Discord commands, independent of persistence."""

    def get(self, user_id: int | str) -> SessionRecord: ...

    def logged_in(self, user_id: int | str) -> bool: ...

    def token(self, user_id: int | str) -> str | None: ...

    def lang(self, user_id: int | str) -> str | None: ...

    def set_lang(self, user_id: int | str, lang: str) -> None: ...

    def set_token(self, user_id: int | str, token: str) -> None: ...

    def clear_token(self, user_id: int | str) -> None: ...


class DatabaseSessionStore:
    """Loop-local session snapshot backed by ordered async SQLite writes."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.records: dict[str, SessionRecord] = {}
        self._writes: asyncio.Task[None] | None = None
        self.tasks = TaskRegistry()

    async def start(self, legacy: Path) -> None:
        from threadutil import named_to_thread

        payload = await self.db.get_metadata("discord_sessions")
        if payload is None and await named_to_thread(
            "files:discord-session-exists", legacy.is_file
        ):
            payload = await named_to_thread(
                "files:discord-session-import", legacy.read_text, encoding="utf-8"
            )
        raw: object = json.loads(payload or "{}")
        if not isinstance(raw, dict):
            raise ValueError("Discord sessions must be an object")
        for key, value in cast("dict[str, object]", raw).items():
            if isinstance(value, dict):
                value = cast("dict[str, object]", value)
                token, lang = value.get("token"), value.get("lang")
                self.records[str(key)] = SessionRecord(
                    str(key),
                    token if isinstance(token, str) else "",
                    lang if isinstance(lang, str) and lang in {"ru", "en"} else None,
                )
        # Only mark import complete after the entire payload was read successfully.
        await self.db.set_metadata("discord_sessions", self._payload())

    def _payload(self) -> str:
        return json.dumps(
            {
                key: {"token": record.token, "lang": record.lang}
                for key, record in self.records.items()
            }
        )

    def get(self, user_id: int | str) -> SessionRecord:
        key = str(user_id)
        return self.records.get(key, SessionRecord(key, "", None))

    def logged_in(self, user_id: int | str) -> bool:
        return self.get(user_id).logged_in

    def token(self, user_id: int | str) -> str | None:
        return self.get(user_id).token or None

    def lang(self, user_id: int | str) -> str | None:
        return self.get(user_id).lang

    def clear_token(self, user_id: int | str) -> None:
        self.set_token(user_id, "")

    def _save(self, record: SessionRecord) -> None:
        self.records[record.user_id] = record
        previous, payload = self._writes, self._payload()

        async def write() -> None:
            if previous is not None:
                # Preserve ordering, but let the latest snapshot recover from a
                # failed write. TaskRegistry already reports the earlier error.
                with suppress(Exception):
                    await previous
            await self.db.set_metadata("discord_sessions", payload)

        self._writes = self.tasks.create(write, name="discord:session-write")

    def set_lang(self, user_id: int | str, lang: str) -> None:
        if lang in {"ru", "en"}:
            old = self.get(user_id)
            self._save(SessionRecord(old.user_id, old.token, lang))

    def set_token(self, user_id: int | str, token: str) -> None:
        old = self.get(user_id)
        self._save(SessionRecord(old.user_id, token, old.lang))

    async def flush(self) -> None:
        if self._writes is not None:
            await self._writes

    async def settle(self) -> None:
        await self.tasks.drain()
        await self.flush()
