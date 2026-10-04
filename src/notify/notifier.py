"""Deliver one localized warning and remember it only after the send works."""
from __future__ import annotations

from collections.abc import Callable, Mapping

from loggers import Logger

from .events import Notice
from .kinds import Scope

Send = Callable[[int, str], None]
Language = Callable[[int], str]
Texts = Mapping[str, Mapping[str, str]]


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
    kind, _sep, bucket = marker.partition(":")
    if not kind.isidentifier() or (_sep and not bucket):
        return None
    return username, marker


class Notifier:
    """Public-bot notification channel.

    ``seen`` is the cycle's stored keys (``username:kind:bucket``). The
    notifier mutates that same set, so a successful send is what the caller
    persists. Markers are per username, not per Telegram id.
    """

    def __init__(
        self,
        *,
        texts: Texts,
        send: Send,
        language: Language,
        log: Logger | None = None,
    ) -> None:
        self._texts = texts
        self._send = send
        self._language = language
        self._log = log or Logger(type(self).__name__)

    def notify(
        self,
        username: str,
        telegram_id: int | None,
        notice: Notice,
        seen: set[str],
    ) -> bool:
        """Send ``notice`` unless this account already heard this bucket.

        Returns True only when Telegram accepted the message. A missing
        mapping, a missing string, or a send error leaves ``seen`` unchanged
        so the next cycle retries.
        """
        key = account_key(username, notice.marker)
        if telegram_id is None:
            return False
        if key in seen:
            return False
        if notice.kind.scope is Scope.ONCE and not _had_episode(username, notice, seen):
            return False
        text = self._render(telegram_id, notice)
        if text is None:
            return False
        try:
            self._send(telegram_id, text)
        except Exception as error:
            self._log.error(
                "failed to send %s to %s (%s): %s",
                notice.kind.kind_id, username, telegram_id, error,
                exc_info=True,
            )
            return False
        seen.add(key)
        # A new outage makes the next recovery newsworthy again.
        recovery = notice.kind.opens_recovery
        if recovery is not None:
            seen.discard(account_key(username, recovery.kind_id))
        for prefix in notice.clears:
            stale = _account_kind(username, prefix, seen)
            for marker in stale:
                seen.discard(marker)
        return True

    def _render(self, telegram_id: int, notice: Notice) -> str | None:
        lang = self._language(telegram_id)
        table = self._texts.get(lang) or self._texts.get("en") or {}
        template = table.get(notice.kind.text_key)
        if not template:
            self._log.error(
                "missing notification text %s for %s",
                notice.kind.text_key, lang,
            )
            return None
        try:
            return template.format_map(_Fields(notice.fields))
        except (KeyError, IndexError, ValueError) as error:
            self._log.error(
                "failed to format %s: %s",
                notice.kind.text_key, error,
            )
            return None


class _Fields(dict[str, int]):
    """Format map that rejects a placeholder the notice did not supply."""

    def __missing__(self, key: str) -> int:
        raise KeyError(key)


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
