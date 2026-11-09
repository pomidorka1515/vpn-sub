from __future__ import annotations

from typing import cast

SQLITE_MAX_INT = 2**63 - 1
MAX_GB = SQLITE_MAX_INT // 10**9


def bounded_int(
    name: str,
    raw: object,
    *,
    minimum: int = 0,
    maximum: int = SQLITE_MAX_INT,
) -> int:
    """Accept integer representations without truncation, within safe bounds."""
    if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
        raise ValueError(f"{name} must be a finite integer")
    try:
        value = int(cast(str, raw))
    except ValueError, TypeError, OverflowError:
        raise ValueError(f"{name} must be a finite integer") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be in range {minimum} to {maximum}")
    return value
