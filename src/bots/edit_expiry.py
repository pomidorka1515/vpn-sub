"""Shared parsing for administrator bot expiry edits."""

from __future__ import annotations

from datetime import UTC, datetime


def parse_edit_expiry(value: str) -> int:
    """Parse a UTC date and time, or zero for an unlimited subscription."""
    value = value.strip()
    if value == "0":
        return 0
    try:
        date = datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        timestamp = int(date.timestamp())
    except (ValueError, OverflowError, OSError) as error:
        raise ValueError("Invalid expiry") from error
    if timestamp <= 0:
        raise ValueError("Invalid expiry")
    return timestamp
