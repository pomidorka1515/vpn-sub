from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loggers import Logger

from .kinds import Scope

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from .events import Notice


type Texts = Mapping[str, Mapping[str, str]]


class Notifier:
    """Serialize delivery; callers persist accepted markers before releasing ownership."""

    def __init__(
        self,
        *,
        texts: Texts,
        send: Callable[[int, str], Awaitable[object]],
        language: Callable[[int], Awaitable[str]],
    ) -> None:
        self._texts = texts
        self._log = Logger(type(self).__name__)
        self._async_send = send
        self._async_language = language
        self._delivery_lock = asyncio.Lock()

    async def deliver(
        self, username: str, telegram_id: int | None, notice: Notice, seen: set[str],
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
                    notice.kind.text_key,
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


def account_key(username: str, marker: str) -> str:
    """Stored kind. The account is part of the key, not the Telegram id."""
    return f"{username}:{marker}"


def split_key(stored: str) -> tuple[str, str] | None:
    """``username:kind:bucket`` into ``(username, kind:bucket)``.

    The username is the first field. A prefix search would also claim
    ``alice`` markers for an account named ``alic``.
    A recovery marker has no bucket, so ``kind`` alone is valid.
    """
    username, separator, marker = stored.partition(":")
    if not separator or not username or not marker or marker.startswith(":"):
        return None
    kind, sep, bucket = marker.partition(":")
    if not kind.isidentifier() or (sep and not bucket):
        return None
    return username, marker


def _account_kind(username: str, kind_id: str, seen: set[str]) -> list[str]:
    """Stored keys for this account and kind, with or without a bucket."""
    matched: list[str] = []
    for item in seen:
        parsed = split_key(item)
        if parsed is None or parsed[0] != username:
            continue
        kind, _sep, _bucket = parsed[1].partition(":")
        if kind == kind_id:
            matched.append(item)
    return matched


def _had_episode(username: str, notice: Notice, seen: set[str]) -> bool:
    """A recovery is only news if this account was told about the outage."""
    return any(_account_kind(username, prefix, seen) for prefix in notice.clears)


__all__ = ["Notifier", "account_key", "split_key"]
