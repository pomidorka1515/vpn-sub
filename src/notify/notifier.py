from __future__ import annotations

import asyncio
import logging
import time
import weakref
from typing import TYPE_CHECKING

from loggers import Logger

from .kinds import Kind, Scope

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from .events import Notice


type Texts = Mapping[str, Mapping[str, str]]

_diagnostics = logging.getLogger("notification.templates")
_diagnostics.propagate = False
if not _diagnostics.handlers:
    _diagnostics.addHandler(logging.StreamHandler())


class Notifier:
    """Serialize each account's delivery and bound external waits."""

    def __init__(
        self,
        *,
        texts: Texts,
        send: Callable[[int, str], Awaitable[object]],
        language: Callable[[int], Awaitable[str]],
        timeout: float = 10.0,
        concurrency: int = 4,
    ) -> None:
        if timeout <= 0 or concurrency < 1:
            raise ValueError("notification timeout and concurrency must be positive")
        self._texts = texts
        self._log = Logger(type(self).__name__)
        self._async_send = send
        self._async_language = language
        self._timeout = timeout
        self._delivery_slots = asyncio.Semaphore(concurrency)
        self._delivery_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._template_diagnostics: dict[tuple[str, str], float] = {}

    def _template_error(self, kind: str, reason: str) -> None:
        key = (kind, reason)
        now = time.monotonic()
        if now >= self._template_diagnostics.get(key, 0):
            self._template_diagnostics[key] = now + 300
            _diagnostics.warning(
                "Notification template %s is %s; check language configuration", kind, reason
            )

    @staticmethod
    def pending(username: str, notice: Notice, seen: set[str]) -> bool:
        """Whether this notice still needs delivery, independent of sender availability."""
        return account_key(username, notice.marker) not in seen and (
            notice.kind.scope is not Scope.ONCE or _had_episode(username, notice, seen)
        )

    async def deliver(
        self,
        username: str,
        telegram_id: int | None,
        notice: Notice,
        seen: set[str],
        *,
        validate: Callable[[], Awaitable[bool]] | None = None,
    ) -> bool:
        async with self._delivery_locks.setdefault(username, asyncio.Lock()), self._delivery_slots:
            key = account_key(username, notice.marker)
            if telegram_id is None or not self.pending(username, notice, seen):
                return False
            try:
                lang = await asyncio.wait_for(self._async_language(telegram_id), self._timeout)
                template = self._texts.get(lang, {}).get(notice.kind.text_key)
                if template is None:
                    template = self._texts.get("en", {}).get(notice.kind.text_key)
                if not template:
                    self._template_error(notice.kind.text_key, "missing")
                    return False
                try:
                    text = template.format_map(notice.fields)
                except ValueError, KeyError, IndexError, AttributeError, TypeError:
                    self._template_error(notice.kind.text_key, "invalid")
                    return False
                if validate is not None and not await validate():
                    return False
                async with asyncio.timeout(self._timeout):
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
    terminal = {kind.kind_id for kind in Kind if kind.scope is Scope.EPISODE}
    return any(
        _account_kind(username, prefix, seen) for prefix in notice.clears if prefix in terminal
    )


__all__ = ["Notifier", "account_key", "split_key"]
