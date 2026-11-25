from __future__ import annotations

from typing import TYPE_CHECKING

from obfuscator import no_rename

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


# NOTE: all exceptions need to have @no_rename for safety

# ------------------------------------------------------------
# Main exceptions
# ------------------------------------------------------------
@no_rename
class AppError(Exception):
    """Base app exception class."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@no_rename
class ValidationError(AppError):
    pass


@no_rename
class CommittedAuditError(AppError):
    """The mutation succeeded; only its subsequent audit append failed."""

    def __init__(self, *, result: JsonValue = None) -> None:
        super().__init__(
            "Mutation committed; audit append pending. Do not retry the mutation.",
            status=202,
        )
        self.result = result


@no_rename
class NotFoundError(AppError):
    def __init__(self, message: str = "Not found") -> None:
        super().__init__(message, status=404)


@no_rename
class ConflictError(AppError):
    def __init__(self, message: str = "Already exists") -> None:
        super().__init__(message, status=409)


# ------------------------------------------------------------
# Misc
# ------------------------------------------------------------

@no_rename
class UnsupportedPlatformError(AppError):
    def __init__(self, message: str = "Unsupported platform; run this app on Linux.") -> None:
        super().__init__(message, status=501)


# ------------------------------------------------------------
# `config.py`
# ------------------------------------------------------------
@no_rename
class ConfigError(AppError):
    pass


@no_rename
class SchemaValidationError(ConfigError):
    def __init__(self, message: str = "Schema failed to validate.") -> None:
        super().__init__(message, status=400)


@no_rename
class FileCorruptionError(ConfigError):
    def __init__(self, message: str = "Config JSON cannot be decoded.") -> None:
        super().__init__(message, status=400)


@no_rename
class ReadOnlyConfigError(ConfigError):
    def __init__(self, message: str = "Attempted to mutate read-only config.") -> None:
        super().__init__(message, status=400)


# ------------------------------------------------------------
# `db.py`
# ------------------------------------------------------------
@no_rename
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


@no_rename
class DuplicateError(DatabaseError):
    def __init__(self, message: str = "A unique value provided is already in use.") -> None:
        super().__init__(message, status=400, public_message=message)


@no_rename
class CodeError(DatabaseError):
    def __init__(
        self,
        message: str = "The provided code is either invalid, expired or exhausted.",
    ) -> None:
        super().__init__(message, status=400, public_message=message)


@no_rename
class MigrationError(DatabaseError):
    def __init__(self, message: str = "Legacy data could not be parsed or imported.") -> None:
        super().__init__(message, status=500)


# ------------------------------------------------------------
# `session/` / 3X-UI related
# ------------------------------------------------------------

@no_rename
class XUiSessionError(AppError):
    pass


@no_rename
class PanelUnavailableError(XUiSessionError):
    def __init__(self, message: str = "Panel operation failed") -> None:
        super().__init__(message, status=502)


@no_rename
class NoPanelsConfiguredError(PanelUnavailableError):
    def __init__(self) -> None:
        super().__init__("No panels configured")
        self.status = 503


@no_rename
class PanelRejectedError(PanelUnavailableError):
    def __init__(self, message: str = "Panel rejected operation") -> None:
        super().__init__(message)
