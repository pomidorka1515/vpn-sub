from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import JsonValue

__all__ = (
    "AppError",
    "CodeError",
    "CommittedAuditError",
    "ConfigError",
    "ConflictError",
    "DatabaseError",
    "DuplicateError",
    "FileCorruptionError",
    "MigrationError",
    "NoPanelsConfiguredError",
    "NotFoundError",
    "PanelRejectedError",
    "PanelUnavailableError",
    "ReadOnlyConfigError",
    "SchemaValidationError",
    "UnsupportedPlatformError",
    "ValidationError",
    "XUiSessionError",
)


# ------------------------------------------------------------
# Main exceptions
# ------------------------------------------------------------
class AppError(Exception):
    """Base app exception class."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class ValidationError(AppError):
    pass


class CommittedAuditError(AppError):
    """The mutation succeeded; only its subsequent audit append failed."""

    def __init__(self, *, result: JsonValue = None) -> None:
        super().__init__(
            "Mutation committed; audit append pending. Do not retry the mutation.",
            status=202,
        )
        self.result = result


class NotFoundError(AppError):
    def __init__(self, message: str = "Not found") -> None:
        super().__init__(message, status=404)


class ConflictError(AppError):
    def __init__(self, message: str = "Already exists") -> None:
        super().__init__(message, status=409)


# ------------------------------------------------------------
# Misc
# ------------------------------------------------------------


class UnsupportedPlatformError(AppError):
    def __init__(self, message: str = "Unsupported platform; run this app on Linux.") -> None:
        super().__init__(message, status=501)


# ------------------------------------------------------------
# `config.py`
# ------------------------------------------------------------
class ConfigError(AppError):
    pass


class SchemaValidationError(ConfigError):
    def __init__(self, message: str = "Schema failed to validate.") -> None:
        super().__init__(message, status=400)


class FileCorruptionError(ConfigError):
    def __init__(self, message: str = "Config JSON cannot be decoded.") -> None:
        super().__init__(message, status=400)


class ReadOnlyConfigError(ConfigError):
    def __init__(self, message: str = "Attempted to mutate read-only config.") -> None:
        super().__init__(message, status=400)


# ------------------------------------------------------------
# `db.py`
# ------------------------------------------------------------
class DatabaseError(AppError):
    def __init__(
        self,
        message: str = "Database operation failed",
        *,
        status: int = 500,
        public_message: str = "Database operation failed",
    ) -> None:
        # Keep diagnostics in the exception/traceback, not in the HTTP response.
        super().__init__(message, status=status)
        self.message = public_message


class DuplicateError(DatabaseError):
    def __init__(self, message: str = "A unique value provided is already in use.") -> None:
        super().__init__(message, status=400, public_message=message)


class CodeError(DatabaseError):
    def __init__(
        self,
        message: str = "The provided code is either invalid, expired or exhausted.",
    ) -> None:
        super().__init__(message, status=400, public_message=message)


class MigrationError(DatabaseError):
    def __init__(self, message: str = "Legacy data could not be parsed or imported.") -> None:
        super().__init__(message, status=500)


# ------------------------------------------------------------
# `session/` / 3X-UI related
# ------------------------------------------------------------


class XUiSessionError(AppError):
    pass


class PanelUnavailableError(XUiSessionError):
    def __init__(self, message: str = "Panel operation failed") -> None:
        super().__init__(message, status=502)


class NoPanelsConfiguredError(PanelUnavailableError):
    def __init__(self) -> None:
        super().__init__("No panels configured")
        self.status = 503


class PanelRejectedError(PanelUnavailableError):
    def __init__(self, message: str = "Panel rejected operation") -> None:
        super().__init__(message)
