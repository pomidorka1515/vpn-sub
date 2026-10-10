"""User-facing Telegram notifications.

Quota code classifies a user row into ``Notice`` values. ``Notifier`` renders
and sends one, and only then is the marker recorded.
"""

from __future__ import annotations

from .events import Notice, classify, restored
from .kinds import Kind, Scope
from .notifier import Notifier, account_key, split_key

__all__ = [
    "Kind",
    "Notice",
    "Notifier",
    "Scope",
    "account_key",
    "classify",
    "restored",
    "split_key",
]
