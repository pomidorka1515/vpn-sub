from __future__ import annotations

from errors import AppError


class SessionExpiredError(AppError):
    """The persisted Discord token no longer resolves to an account."""
    def __init__(self) -> None:
        super().__init__("Session expired", status=401)
