from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, MutableMapping
from loggers import Logger
from typing import (
    Any, Self, Literal,
    cast
)
from types import TracebackType
from contextlib import contextmanager
import copy
import glob
import json
import os
import tempfile
import threading
import io
from datetime import datetime

import jsonschema

try:
    import fcntl
except ModuleNotFoundError as exc:
    raise RuntimeError("Run this on linux.") from exc

__all__ = [
    'ConfigError', 'SchemaValidationError', 'FileCorruptionError',
    'ReadOnlyConfigError', 'Config', 'LinesConfig'
]

SYNC_MODES = Literal['full', 'data', 'none']

# ---------------------------------------------------------------------------
# Module-level file primitives (shared by Config and LinesConfig)
# ---------------------------------------------------------------------------

def _ensure_parent_dir(path: str) -> None:
    dir_path = os.path.dirname(path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)

def _lockfile_path(path: str) -> str:
    return f"{path}.lock"

@contextmanager
def _locked_file(path: str, *, exclusive: bool) -> Iterator[None]:
    _ensure_parent_dir(path)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    with open(_lockfile_path(path), "a+b") as lock_fp:
        fcntl.flock(lock_fp, mode)
        try:
            yield
        finally:
            fcntl.flock(lock_fp, fcntl.LOCK_UN)

def _stat_signature(path: str) -> tuple[int, int, int, int] | None:
    try:
        stat_result = os.stat(path)
    except FileNotFoundError:
        return None
    return (
        stat_result.st_mtime_ns,
        stat_result.st_size,
        stat_result.st_ino,
        stat_result.st_dev,
    )

def _file_signature(path: str) -> tuple[int, int, int, int] | None:
    return _stat_signature(path)

def _fsync_parent_dir(path: str) -> None:
    dir_path = os.path.dirname(path) or "."
    dir_fd = os.open(dir_path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

def _atomic_write_json(
    path: str,
    data: dict[str, Any],
    *,
    indent: int,
    sync_mode: SYNC_MODES,
) -> tuple[int, int, int, int] | None:
    """Write atomically: temp file + os.replace. Returns new file signature."""
    _ensure_parent_dir(path)
    dir_path = os.path.dirname(path) or "."

    fd, temp_path = tempfile.mkstemp(dir=dir_path, prefix=".tmp_", suffix=".json")
    os.close(fd)

    try:
        if os.path.exists(path):
            existing = os.stat(path, follow_symlinks=False)
            os.chmod(temp_path, existing.st_mode & 0o777)
            try:
                os.chown(temp_path, existing.st_uid, existing.st_gid)
            except PermissionError:
                pass

        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=indent, ensure_ascii=False)
            handle.write("\n")

            if sync_mode != "none":
                handle.flush()
                os.fsync(handle.fileno())

        os.replace(temp_path, path)

        if sync_mode == "full":
            _fsync_parent_dir(path)

    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise

    return _file_signature(path)

def _instance_backup_dir(path: str, backup_dir: str) -> str:
    name = os.path.splitext(os.path.basename(path))[0]
    return os.path.join(backup_dir, name)

def _do_backup(
    path: str,
    indent: int,
    instance_dir: str,
    log: Logger,
) -> None:
    """Take a snapshot and write it to a per-instance subdirectory."""
    os.makedirs(instance_dir, exist_ok=True)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = os.path.join(instance_dir, f"{timestamp}.json")

    fd, tmp = tempfile.mkstemp(dir=instance_dir, prefix=".tmp-", suffix=".tmp")
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
        os.replace(tmp, backup_path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise

    log.debug(f"backup saved: {backup_path}")

def _prune_backups(instance_dir: str, retention: int, log: Logger) -> None:
    """Keep only the N most recent backups."""
    pattern = os.path.join(instance_dir, "*.json")
    files = sorted(glob.glob(pattern))
    to_delete = files[:-retention]
    for f in to_delete:
        try:
            os.unlink(f)
            log.debug(f"pruned old backup: {f}")
        except OSError as e:
            log.error(f"prune failed for {f}: {e}")

def _make_backup_thread(
    *, # NOTE: kwarg only for safety
    path: str,
    indent: int,
    backup_dir: str,
    backup_interval: int | float,
    backup_retention: int,
    stop_event: threading.Event,
) -> threading.Thread:
    def loop() -> None:
        log = Logger("Backup")
        instance_dir = _instance_backup_dir(path, backup_dir)
        while not stop_event.wait(backup_interval):
            try:
                _do_backup(path, indent, instance_dir, log)
                _prune_backups(instance_dir, backup_retention, log)
                log.info("backup successful")
            except Exception as e:
                log.error(f"backup failed: {e}")
    return threading.Thread(target=loop, daemon=True, name="Backup")

class ConfigError(RuntimeError):
    """Base config manager error."""


class SchemaValidationError(ConfigError):
    """Raised when config data violates its JSON schema."""


class FileCorruptionError(ConfigError):
    """Raised when config JSON cannot be decoded."""

class ReadOnlyConfigError(ConfigError):
    """Raised when trying to mutate a read-only config instance."""

class Config(MutableMapping[str, Any]):
    """
    Thread-safe, process-safe JSON config manager.

    Notes:
    - Uses a dedicated `.lock` file for inter-process locking.
    - Uses atomic replace for writes.
    - `with cfg as tx:` returns a transaction object backed by a working copy.
    - Top-level JSON must be an object.
    """

    MISSING = object()

    def __init__(
        self,
        path: str,
        indent: int = 4,
        read_only: bool = False,
        strict_schema: bool = True,
        sync_mode: SYNC_MODES = 'data',
        isolate_commits: bool = True,
        backup_dir: str | None = None,
        backup_interval: int | float = 7200,
        backup_retention: int = 3
    ) -> None:
        """
        Args:
            path: Path to the JSON config file (created if missing).
            indent: JSON indentation width for writes.
            read_only: Read-only, raises on writes. Handle with caution.
                WARNING: This arg is also immutable.
                You cannot make a Config() instance read-only past __init__ and vise versa.
            strict_schema: If True, schema errors raise; if False, they log a warning.
            sync_mode: 'full' fsyncs file + parent directory, 'data' fsyncs file only,
                'none' skips fsync entirely.
            isolate_commits: If True, deep-copies data after commit so any references
                that leaked out of the transaction can't mutate the live config.
            backup_dir: Backup directory. Backups are disabled if set to None.
            backup_interval: Interval in seconds for the backups, in seconds.
            backup_retention: Amount of concurrent backups kept on disk.
        """
        self.log = Logger(type(self).__name__)
        self._path: str = path
        self._indent: int = indent
        self._strict_schema: bool = strict_schema
    
        if sync_mode not in ["full", "data", "none"]:
            raise ValueError("sync_mode must be 'full', 'data', or 'none'")
        self._sync_mode: SYNC_MODES = sync_mode
    
        # True = safer: committed state is detached from leaked tx refs
        # False = faster but unsafe if refs escape the transaction
        self._isolate_commits: bool = isolate_commits
    
        self._data: dict[str, Any] = {}
        self._last_signature: tuple[int, int, int, int] | None = None
    
        self._lock = threading.RLock()
        self._active_transaction: _ConfigTransaction | None = None
        self._context_transaction: _ConfigTransaction | None = None
    
        self._schema_cache_path: str | None = None
        self._schema_cache_signature: tuple[int, int, int, int] | None = None
        self._schema_cache: dict[str, Any] | None = None
    
        self._warned_update_callable: bool = False

        self._read_only: bool = read_only

        self._backup_dir: str | None = backup_dir
        self._backup_interval: int | float = backup_interval
        self._backup_retention: int = backup_retention
        self._backup_stop = threading.Event()

        with self.log.loading():
            self.reload()

        if backup_dir:
            self._backup_t = _make_backup_thread(
                path=self._path,
                indent=self._indent,
                backup_dir=backup_dir,
                backup_interval=backup_interval,
                backup_retention=backup_retention,
                stop_event=self._backup_stop
            )
            self._backup_t.start()
        else:
            self._backup_t: threading.Thread | None = None

    @property
    def path(self):
        return self._path

    def _raise_if_read_only(self) -> None:
        if self._read_only:
            raise ReadOnlyConfigError("Cannot modify read-only config instance")
    
    def reload(self) -> bool:
        """Reload from disk if the file changed since last load. Creates the file if missing.

        Returns:
            True if the data was actually reloaded, False if the file was unchanged.
        """
        with self._lock:
            self._raise_if_used_inside_transaction()
            return self._reload_locked(create_if_missing=True, exclusive=True)

    def edit(self) -> _ConfigTransaction:
        """Open an explicit transaction.

        Usage:
            with cfg.edit() as tx:
                tx["x"].pop("124", None)
                tx["count"] += 1
        """
        self._raise_if_read_only()
        return _ConfigTransaction(self)

    def mutate(self, callback: Callable[[MutableMapping[str, Any]], Any]) -> Any:
        """Run a callback inside a transaction and return its result.

        Keep callbacks short and non-blocking: they run while holding the
        in-process lock and the inter-process file lock.

        Args:
            callback: Called with the transaction mapping as its only argument.

        Returns:
            Deep-copied return value of the callback.
        """
        self._raise_if_read_only()
        with self.edit() as tx:
            result = callback(tx)
        return self._detach(result)

    def __enter__(self) -> _ConfigTransaction:
        self._raise_if_read_only()
        tx = self.edit()
        self._context_transaction = tx
        try:
            return tx.__enter__()
        except Exception:
            self._context_transaction = None
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        tx = self._context_transaction
        self._context_transaction = None
        if tx is None:
            raise RuntimeError("Config.__exit__ called without a matching __enter__().")
        return tx.__exit__(exc_type, exc_val, exc_tb)

    def __getitem__(self, key: str) -> Any:
        with self._lock:
            self._raise_if_used_inside_transaction()
            self._ensure_recent_locked()
            return self._detach(self._data[key])

    def __setitem__(self, key: str, value: Any) -> None:
        self._raise_if_read_only()
        self._run_edit(lambda tx: tx.__setitem__(key, value))

    def __delitem__(self, key: str) -> None:
        self._raise_if_read_only()
        self._run_edit(lambda tx: tx.__delitem__(key))

    def __contains__(self, key: object) -> bool:
        with self._lock:
            self._raise_if_used_inside_transaction()
            self._ensure_recent_locked()
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            self._raise_if_used_inside_transaction()
            self._ensure_recent_locked()
            return len(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            self._raise_if_used_inside_transaction()
            self._ensure_recent_locked()
            return self._detach(self._data.get(key, default))

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            self._raise_if_used_inside_transaction()
            self._ensure_recent_locked()
            return iter(tuple(self._data.keys()))
    
    def keys(self) -> tuple[Any, ...]: # pyright: ignore[reportIncompatibleMethodOverride]
        with self._lock:
            self._raise_if_used_inside_transaction()
            self._ensure_recent_locked()
            return tuple(self._data.keys())
    
    def values(self) -> tuple[Any, ...]: # pyright: ignore[reportIncompatibleMethodOverride]
        with self._lock:
            self._raise_if_used_inside_transaction()
            self._ensure_recent_locked()
            return tuple(self._detach(v) for v in self._data.values())
    
    def items(self) -> tuple[tuple[str, Any], ...]: # pyright: ignore[reportIncompatibleMethodOverride]
        with self._lock:
            self._raise_if_used_inside_transaction()
            self._ensure_recent_locked()
            return tuple((k, self._detach(v)) for k, v in self._data.items())

    def copy(self) -> dict[str, Any]:
        """Return a deep copy of the current config data as a plain dict."""
        with self._lock:
            self._raise_if_used_inside_transaction()
            self._ensure_recent_locked()
            return copy.deepcopy(self._data)

    def clear(self) -> None:
        self._raise_if_read_only()
        self._run_edit(lambda tx: tx.clear())

    def pop(self, key: str, default: Any = MISSING) -> Any:
        self._raise_if_read_only()
        if default is self.MISSING:
            return self._run_edit(lambda tx: tx.pop(key))
        return self._run_edit(lambda tx: tx.pop(key, default))

    def popitem(self) -> tuple[str, Any]:
        self._raise_if_read_only()
        return self._run_edit(lambda tx: tx.popitem())

    def setdefault(self, key: str, default: Any = None) -> Any:
        self._raise_if_read_only()
        return self._run_edit(lambda tx: tx.setdefault(key, default))

    def update(self, *args: Any, **kwargs: Any) -> None:
        """Atomic mapping-style update.

        Backward compatibility:
            cfg.update(lambda tx: ...)
        still works, but `mutate()` / `edit()` is clearer.
        """
        self._raise_if_read_only()
        if len(args) == 1 and callable(args[0]) and not kwargs:
            if not self._warned_update_callable:
                self.log.warning(
                    "Config.update(callable) is deprecated; use mutate() or edit()."
                )
                self._warned_update_callable = True
            self.mutate(args[0])
            return

        updates = dict(*args, **kwargs)
        self._run_edit(lambda tx: tx.update(updates))

    def _run_edit(self, action: Callable[[_ConfigTransaction], Any]) -> Any:
        with self.edit() as tx:
            result = action(tx)
        return self._detach(result)

    def _raise_if_used_inside_transaction(self) -> None:
        """Raise if the calling thread already owns an active transaction.

        Direct Config access (e.g. cfg["key"]) inside a transaction block would
        bypass the working copy and cause a stale-read / lost-write hazard.
        """
        tx = self._active_transaction
        if tx is None:
            return
        if tx.owner_thread_id == threading.get_ident():
            raise RuntimeError(
                "Use the transaction object returned by 'with cfg as tx' "
                "while a batch edit is active."
            )

    def _read_json_object(self) -> dict[str, Any]:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            raise
        except json.JSONDecodeError as exc:
            raise FileCorruptionError(
                f"Config file '{self._path}' is not valid JSON: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise ConfigError(
                f"Config file '{self._path}' must contain a JSON object at the top level."
            )
        return cast(dict[str, Any], data)

    def _load_schema(self, data: dict[str, Any]) -> dict[str, Any] | None:
        schema_ref = data.get("$schema")
        if not schema_ref:
            return None

        if not isinstance(schema_ref, str):
            raise SchemaValidationError("'$schema' must be a string.")

        if schema_ref.startswith(("http://", "https://")):
            message = (
                "Remote JSON schemas are not supported for security/reliability reasons."
            )
            if self._strict_schema:
                raise SchemaValidationError(message)
            self.log.warning(message)
            return None

        schema_path = os.path.normpath(
            os.path.join(os.path.dirname(self._path), schema_ref)
        )
        schema_sig = _stat_signature(schema_path)
    
        if (
            schema_path == self._schema_cache_path
            and schema_sig == self._schema_cache_signature
        ):
            return self._schema_cache
    
        try:
            with open(schema_path, "r", encoding="utf-8") as handle:
                schema = json.load(handle)
        except FileNotFoundError as exc:
            if self._strict_schema:
                raise SchemaValidationError(
                    f"Schema file not found: {schema_path}"
                ) from exc
            self.log.warning(f"Schema file not found: {schema_path}")
            return None
        except json.JSONDecodeError as exc:
            raise SchemaValidationError(
                f"Schema file '{schema_path}' is not valid JSON: {exc}"
            ) from exc
    
        if not isinstance(schema, dict):
            raise SchemaValidationError(
                f"Schema file '{schema_path}' must contain a JSON object."
            )
    
        self._schema_cache_path = schema_path
        self._schema_cache_signature = schema_sig
        self._schema_cache = schema
        return cast(dict[Any, Any], schema)

    def _validate_schema(self, data: dict[str, Any]) -> None:
        schema = self._load_schema(data)
        if schema is None:
            return

        try:
            jsonschema.validate(data, schema)
        except jsonschema.ValidationError as exc:
            message = (
                f"Schema validation error: {exc.message} "
                f"at path {list(exc.absolute_path)}"
            )
            if self._strict_schema:
                raise SchemaValidationError(message) from exc
            self.log.warning(message)

    def _reload_locked(self, *, create_if_missing: bool, exclusive: bool) -> bool:
        with _locked_file(self._path, exclusive=exclusive):
            signature = _file_signature(self._path)

            if signature is None:
                if not create_if_missing:
                    raise FileNotFoundError(self._path)

                empty: dict[str, Any] = {}
                self._validate_schema(empty)
                _atomic_write_json(
                    self._path, empty,
                    indent=self._indent, sync_mode=self._sync_mode,
                )

                new_signature = _file_signature(self._path)
                if new_signature is None:
                    raise ConfigError("Config file disappeared immediately after create.")

                self._data = {}
                self._last_signature = new_signature
                return True

            if signature == self._last_signature:
                return False

            data = self._read_json_object()
            self._validate_schema(data)
            self._data = data
            self._last_signature = signature
            return True

    def _ensure_recent_locked(self) -> None:
        """Reload from disk under a shared lock if the file has changed.

        Called before every read to guarantee consistency in multi-process
        deployments where another worker may have committed a write.
        """
        with _locked_file(self._path, exclusive=False):
            signature = _file_signature(self._path)
            if signature is None:
                raise ConfigError(
                    f"Config file '{self._path}' disappeared while in use."
                )

            if signature == self._last_signature:
                return

            data = self._read_json_object()
            self._validate_schema(data)
            self._data = data
            self._last_signature = signature

    def _atomic_write(self, data: dict[str, Any]) -> None:
        self._raise_if_read_only()
        _atomic_write_json(
            self._path, data,
            indent=self._indent, sync_mode=self._sync_mode,
        )

    @staticmethod
    def _detach(value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return copy.deepcopy(cast(dict[Any, Any], value))
        return value

# pyright: reportPrivateUsage=false
# mypy: disable-error-code="attr-defined"
# pylint: disable=protected-access

class _ConfigTransaction(MutableMapping[str, Any]):
    """A batch edit working copy.

    Values returned here are live within the transaction on purpose, so nested
    mutations work. The committed config receives a deep-copied snapshot on
    success, so leaked references do not keep mutating the live config after
    the transaction ends.
    """

    def __init__(self, config: Config) -> None:
        self._config: Config = config
        self.data: dict[str, Any] | None = None
        self.original: dict[str, Any] | None = None
        self._lock_fp: io.BufferedRandom | None = None
        self.owner_thread_id: int | None = None
    
    def __enter__(self) -> Self:
        cfg: Config = self._config
        cfg._lock.acquire()
        lock_fp: io.BufferedRandom | None = None

        try:
            if cfg._active_transaction is not None:
                raise RuntimeError("Nested batch edits are not supported.")

            _ensure_parent_dir(cfg._path)
            lock_fp = open(_lockfile_path(cfg._path), "a+b")
            fcntl.flock(lock_fp, fcntl.LOCK_EX)

            signature = _file_signature(cfg._path)

            if signature is None:
                current: dict[str, Any] = {}
                cfg._validate_schema(current)
                _atomic_write_json(
                    cfg._path, current,
                    indent=cfg._indent, sync_mode=cfg._sync_mode,
                )
                signature = _file_signature(cfg._path)
                if signature is None:
                    raise ConfigError("Config file disappeared immediately after create.")
                cfg._data = current
                cfg._last_signature = signature

            elif signature == cfg._last_signature:
                current = copy.deepcopy(cfg._data)

            else:
                current = cfg._read_json_object()
                cfg._validate_schema(current)
                cfg._data = current
                cfg._last_signature = signature

            self.original = copy.deepcopy(current)
            self.data = copy.deepcopy(current)
            self.owner_thread_id = threading.get_ident()
            cfg._active_transaction = self
            self._lock_fp = lock_fp
            return self

        except Exception:
            try:
                if lock_fp is not None:
                    fcntl.flock(lock_fp, fcntl.LOCK_UN)
                    lock_fp.close()
            finally:
                self._lock_fp = None
                self.data = None
                self.original = None
                self.owner_thread_id = None
                cfg._active_transaction = None
                cfg._lock.release()
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        cfg = self._config

        try:
            if exc_type is None:
                if self.data is None or self.original is None:
                    raise RuntimeError("Transaction is not active.")

                if self.data != self.original:
                    cfg._validate_schema(self.data)
                    cfg._atomic_write(self.data)

                    signature = _file_signature(cfg._path)
                    if signature is None:
                        raise ConfigError(
                            "Config file disappeared immediately after commit."
                        )

                    if cfg._isolate_commits:
                        cfg._data = copy.deepcopy(self.data)
                    else:
                        cfg._data = self.data

                    cfg._last_signature = signature
                else:
                    cfg._data = self.original

            return False

        finally:
            try:
                if self._lock_fp is not None:
                    fcntl.flock(self._lock_fp, fcntl.LOCK_UN)
                    self._lock_fp.close()
            finally:
                self._lock_fp = None
                self.data = None
                self.original = None
                self.owner_thread_id = None
                cfg._active_transaction = None
                cfg._lock.release()

    def _require_active(self) -> dict[str, Any]:
        if self.data is None:
            raise RuntimeError("Transaction is not active.")
        return self.data

    def __getitem__(self, key: str) -> Any:
        return self._require_active()[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._require_active()[key] = value

    def __delitem__(self, key: str) -> None:
        del self._require_active()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._require_active())

    def __len__(self) -> int:
        return len(self._require_active())

    def __contains__(self, key: object) -> bool:
        return key in self._require_active()

    def get(self, key: str, default: Any = None) -> Any:
        return self._require_active().get(key, default)

    def copy(self) -> dict[str, Any]:
        return copy.deepcopy(self._require_active())

    def clear(self) -> None:
        self._require_active().clear()

    def pop(self, key: str, default: Any = Config.MISSING) -> Any:
        data = self._require_active()
        if default is Config.MISSING:
            return data.pop(key)
        return data.pop(key, default)

    def popitem(self) -> tuple[str, Any]:
        return self._require_active().popitem()

    def setdefault(self, key: str, default: Any = None) -> Any:
        return self._require_active().setdefault(key, default)

    def update(self, *args: Any, **kwargs: Any) -> None:
        self._require_active().update(*args, **kwargs)


# ---------------------------------------------------------------------------
# LinesConfig — append-only line-based config (JSONL, NDJSON, etc.)
# ---------------------------------------------------------------------------

class LinesConfig:
    """
    Thread-safe, process-safe line-based config manager.

    Each line is a JSON object. The file is append-only for writes; reads
    can iterate from the beginning or tail. Suitable for JSONL/NDJSON logs.

    Notes:
    - Uses a dedicated `.lock` file for inter-process locking.
    - Sync behavior is consistent with Config (full/data/none via sync_mode).
    - Automatic backup support with configurable interval and retention.
    """

    def __init__(
        self,
        path: str,
        sync_mode: SYNC_MODES = 'data',
        backup_dir: str | None = None,
        backup_interval: int | float = 7200,
        backup_retention: int = 3,
    ) -> None:
        """
        Args:
            path: Path to the line-based file (created if missing).
            sync_mode: 'full' fsyncs file + parent directory, 'data' fsyncs file only,
                'none' skips fsync entirely.
            backup_dir: Backup directory. Backups are disabled if set to None.
            backup_interval: Interval in seconds for backups.
            backup_retention: Amount of concurrent backups kept on disk.
        """
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self._path: str = path
            self._sync_mode: SYNC_MODES = sync_mode
            self._lock = threading.RLock()

            self._backup_dir: str | None = backup_dir
            self._backup_interval: int | float = backup_interval
            self._backup_retention: int = backup_retention
            self._backup_stop = threading.Event()

            _ensure_parent_dir(path)
            if backup_dir:

                self._backup_t = _make_backup_thread(
                    path=self._path,
                    indent=4,
                    backup_dir=backup_dir,
                    backup_interval=backup_interval,
                    backup_retention=backup_retention,
                    stop_event=self._backup_stop
                )
                self._backup_t.start()
            else:
                self._backup_t: threading.Thread | None = None

    @property
    def path(self) -> str:
        return self._path

    def append(self, record: dict[str, Any]) -> None:
        """Append a JSON object as a new line. Thread-safe."""
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            with _locked_file(self._path, exclusive=True):
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line)
                    if self._sync_mode != "none":
                        f.flush()
                        os.fsync(f.fileno())
                if self._sync_mode == "full":
                    _fsync_parent_dir(self._path)

    def append_many(self, records: Iterable[dict[str, Any]]) -> None:
        """Append multiple records in a single atomic write. Thread-safe."""
        lines = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
        with self._lock:
            with _locked_file(self._path, exclusive=True):
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(lines)
                    if self._sync_mode != "none":
                        f.flush()
                        os.fsync(f.fileno())
                if self._sync_mode == "full":
                    _fsync_parent_dir(self._path)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        """Iterate over all lines. Thread-safe at read-time."""
        with self._lock:
            with _locked_file(self._path, exclusive=False):
                try:
                    with open(self._path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                yield json.loads(line)
                except FileNotFoundError:
                    return

    def tail(self, n: int = 100) -> Iterator[dict[str, Any]]:
        """Iterate over the last n lines. Thread-safe at read-time.

        Holds the lock only during the brief yield phase, not during I/O.
        """
        raw_lines = b""
        with _locked_file(self._path, exclusive=False):
            try:
                with open(self._path, "rb") as f:
                    f.seek(0, io.SEEK_END)
                    remaining: int = f.tell()
                    if remaining == 0:
                        return
                    while remaining > 0:
                        chunk_size = min(8192, remaining)
                        f.seek(max(0, remaining - chunk_size))
                        chunk = f.read(chunk_size)
                        remaining -= chunk_size
                        raw_lines = chunk + raw_lines
            except FileNotFoundError:
                return

        # Parse outside the lock — no filesystem I/O, just CPU work
        lines = raw_lines.decode("utf-8").splitlines()
        with self._lock:
            for line in lines[-n:]:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def count(self) -> int:
        """Count total lines. Thread-safe at read-time."""
        with self._lock:
            with _locked_file(self._path, exclusive=False):
                try:
                    with open(self._path, "r", encoding="utf-8") as f:
                        return sum(1 for line in f if line.strip())
                except FileNotFoundError:
                    return 0

    def clear(self) -> None:
        """Truncate the file. Thread-safe."""
        with self._lock:
            with _locked_file(self._path, exclusive=True):
                with open(self._path, "w", encoding="utf-8") as f:
                    if self._sync_mode != "none":
                        f.flush()
                        os.fsync(f.fileno())
                if self._sync_mode == "full":
                    _fsync_parent_dir(self._path)

    def close(self) -> None:
        """Stop backup thread. Does not affect the data file."""
        self._backup_stop.set()
        if self._backup_t is not None:
            self._backup_t.join(timeout=5)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:
        self._backup_stop.set()