from __future__ import annotations

__all__ = (
    "AppError",
    "CodeError",
    "ConfigError",
    "ConflictError",
    "DatabaseError",
    "DuplicateError",
    "FileCorruptionError",
    "MigrationError",
    "NotFoundError",
    "PanelUnavailableError",
    "ReadOnlyConfigError",
    "SchemaValidationError",
    "ValidationError",
    "XUiSessionError"
)

class AppError(Exception):
    """Base app exception class."""
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status

class ValidationError(AppError): pass

class NotFoundError(AppError):
    def __init__(self, message: str = "Not found") -> None: super().__init__(message, status=404)

class ConflictError(AppError):
    def __init__(self, message: str = "Already exists") -> None: super().__init__(message, status=409)

class PanelUnavailableError(AppError):
    def __init__(self, message: str = "Panel operation failed") -> None: super().__init__(message, status=502)

# --------

class ConfigError(AppError): pass

class SchemaValidationError(ConfigError):
    def __init__(self, message: str = "Schema failed to validate.") -> None: super().__init__(message, status=400)

class FileCorruptionError(ConfigError):
    def __init__(self, message: str = "Config JSON cannot be decoded.") -> None: super().__init__(message, status=400)

class ReadOnlyConfigError(ConfigError):
    def __init__(self, message: str = "Attempted to mutate read-only config.") -> None: super().__init__(message, status=400)

# --------

class DatabaseError(AppError): pass

class DuplicateError(DatabaseError):
    def __init__(self, message: str = "A unique value provided is already in use.") -> None: super().__init__(message, status=400)

class CodeError(DatabaseError):
    def __init__(self, message: str = "The provided code is either invalid, expired or exhausted.") -> None: super().__init__(message, status=400)

class MigrationError(DatabaseError):
    def __init__(self, message: str = "Legacy data could not be parsed or imported.") -> None: super().__init__(message, status=500)

# -------

class XUiSessionError(AppError): pass
