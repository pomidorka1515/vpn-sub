from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from .kinds import Scope
from .notifier import Notifier, _account_kind, _had_episode, account_key

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .events import Notice
    from .notifier import Texts


class AsyncNotifier(Notifier):
    """Serialize delivery; callers persist accepted markers before releasing ownership."""

    def __init__(
        self,
        *,
        texts: Texts,
        send: Callable[[int, str], Awaitable[object]],
        language: Callable[[int], Awaitable[str]],
    ) -> None:
        super().__init__(texts=texts, send=lambda _id, _text: None, language=lambda _id: "en")
        self._async_send = send
        self._async_language = language
        self._delivery_lock = asyncio.Lock()

    async def deliver(
        self, username: str, telegram_id: int | None, notice: Notice, seen: set[str]
    ) -> bool:
        async with self._delivery_lock:
            key = account_key(username, notice.marker)
            if telegram_id is None or key in seen:
                return False
            if notice.kind.scope is Scope.ONCE and not _had_episode(username, notice, seen):
                return False
            try:
                lang = await self._async_language(telegram_id)
                template = (self._texts.get(lang) or self._texts.get("en") or {}).get(
                    notice.kind.text_key
                )
                if not template:
                    return False
                text = template.format_map(notice.fields)
                await self._async_send(telegram_id, text)
            except Exception:
                self._log.exception("failed to send %s to %s", notice.kind.kind_id, username)
                return False
            seen.add(key)
            if notice.kind.opens_recovery is not None:
                seen.discard(account_key(username, notice.kind.opens_recovery.kind_id))
            for prefix in notice.clears:
                seen.difference_update(_account_kind(username, prefix, seen))
            return True
