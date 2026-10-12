"""Synchronous delivery contracts for watcher tests pending final retirement."""

from __future__ import annotations

from typing import Protocol


class AdminDelivery(Protocol):
    def msg(self, text: str) -> None: ...


class TelegramDelivery(Protocol):
    def send_message(self, chat_id: int, text: str, *, parse_mode: str) -> object: ...


class PublicDelivery(Protocol):
    TEXTS: dict[str, dict[str, str]]
    bot: TelegramDelivery

    def get_lang(self, uid: int) -> str: ...
