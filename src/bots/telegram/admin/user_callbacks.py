"""Bounded user references for Telegram's 64-byte callback payloads."""

from __future__ import annotations

from base64 import urlsafe_b64encode
from hashlib import sha256


def user_reference(username: str) -> str:
    # ':' cannot occur in an accepted username, so legacy literal callbacks
    # remain unambiguous. Stable references survive pagination and restarts.
    return ":" + urlsafe_b64encode(sha256(username.encode()).digest()).decode().rstrip("=")


def user_callback(prefix: str, username: str) -> str:
    data = f"{prefix}_{username}"
    if len(data.encode()) <= 64:
        return data
    return f"{prefix}_{user_reference(username)}"
