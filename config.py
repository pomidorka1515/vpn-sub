from __future__ import annotations

from collections.abc import Callable, Iterator, MutableMapping
from loggers import Logger
from typing import Any, Self
from types import TracebackType
from contextlib import contextmanager
import copy
import json
import os
import tempfile
import threading

import jsonschema

try:
    import fcntl
except ModuleNotFoundError as exc:
    raise RuntimeError("Run this on linux.") from exc

__all__ = ['ConfigError', 'SchemaValidationError', 'FileCorruptionError', 'Config']

class ConfigError(RuntimeError):
    """Base config manager error."""


class SchemaValidationError(ConfigError):
    """Raised when config data violates its JSON schema."""


class FileCorruptionError(ConfigError):
    """Raised when config JSON cannot be decoded."""


class Config(MutableMapping[str, Any]):
    """Thread-safe, process-safe JSON config manager.

    Notes:
    - Uses a dedicated `.lock` file for inter-process locking.
    - Uses atomic replace for writes.
    - `with cfg as tx:` returns a transaction object backed by a working copy.
    - Top-level JSON must be an object.
    """

    _MISSING = object()

    def __init__(
        self,
        path: str,
        indent: int = 4,
        strict_schema: bool = True,
        sync_mode: str = "data",
        isolate_commits: bool = True,
    ) -> None:
        self.log = Logger(type(self).__name__)
        self._path = path
        self._indent = indent
        self._strict_schema = strict_schema
    
        if sync_mode not in ["full", "data", "none"]:
            raise ValueError("sync_mode must be 'full', 'data', or 'none'")
        self._sync_mode = sync_mode
    
        # True = safer: committed state is detached from leaked tx refs
        # False = faster but unsafe if refs escape the transaction
        self._isolate_commits = isolate_commits
    
        self._data: dict[str, Any] = {}
        self._last_signature: tuple[int, int, int, int] | None = None
    
        self._lock = threading.RLock()
        self._active_transaction: _ConfigTransaction | None = None
        self._context_transaction: _ConfigTransaction | None = None
    
        self._schema_cache_path: str | None = None
        self._schema_cache_signature: tuple[int, int, int, int] | None = None
        self._schema_cache: dict[str, Any] | None = None
    
        self._warned_update_callable = False
    
        with self.log.loading():
            self.reload()

    def reload(self) -> bool:
        """Reload from disk if changed. Creates the file if missing."""
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
        return _ConfigTransaction(self)

    def mutate(self, callback: Callable[[MutableMapping[str, Any]], Any]) -> Any:
        """Run a callback inside a transaction.

        Keep callbacks short and non-blocking: they run while holding the
        in-process lock and the inter-process file lock.
        """
        with self.edit() as tx:
            result = callback(tx)
        return self._detach(result)

    def __enter__(self) -> _ConfigTransaction:
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
        self._run_edit(lambda tx: tx.__setitem__(key, value))

    def __delitem__(self, key: str) -> None:
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
    
    def keys(self):
        with self._lock:
            self._raise_if_used_inside_transaction()
            self._ensure_recent_locked()
            return tuple(self._data.keys())
    
    def values(self):
        with self._lock:
            self._raise_if_used_inside_transaction()
            self._ensure_recent_locked()
            return tuple(self._detach(v) for v in self._data.values())
    
    def items(self):
        with self._lock:
            self._raise_if_used_inside_transaction()
            self._ensure_recent_locked()
            return tuple((k, self._detach(v)) for k, v in self._data.items())

    def copy(self) -> dict[str, Any]:
        with self._lock:
            self._raise_if_used_inside_transaction()
            self._ensure_recent_locked()
            return copy.deepcopy(self._data)

    def clear(self) -> None:
        self._run_edit(lambda tx: tx.clear())

    def pop(self, key: str, default: Any = _MISSING) -> Any:
        if default is self._MISSING:
            return self._run_edit(lambda tx: tx.pop(key))
        return self._run_edit(lambda tx: tx.pop(key, default))

    def popitem(self) -> tuple[str, Any]:
        return self._run_edit(lambda tx: tx.popitem())

    def setdefault(self, key: str, default: Any = None) -> Any:
        return self._run_edit(lambda tx: tx.setdefault(key, default))

    def update(self, *args: Any, **kwargs: Any) -> None:
        """Atomic mapping-style update.

        Backward compatibility:
            cfg.update(lambda tx: ...)
        still works, but `mutate()` / `edit()` is clearer.
        """
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
        tx = self._active_transaction
        if tx is None:
            return
        if tx.owner_thread_id == threading.get_ident():
            raise RuntimeError(
                "Use the transaction object returned by 'with cfg as tx' "
                "while a batch edit is active."
            )

    def _ensure_parent_dir(self) -> None:
        dir_path = os.path.dirname(self._path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

    def _lockfile_path(self) -> str:
        return f"{self._path}.lock"

    @contextmanager
    def _locked_file(self, *, exclusive: bool) -> Iterator[None]:
        self._ensure_parent_dir()
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        with open(self._lockfile_path(), "a+b") as lock_fp:
            fcntl.flock(lock_fp, mode)
            try:
                yield
            finally:
                fcntl.flock(lock_fp, fcntl.LOCK_UN)


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
        return data

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
        schema_sig = self._stat_signature(schema_path)
    
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
        return schema

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
        with self._locked_file(exclusive=exclusive):
            signature = self._file_signature()

            if signature is None:
                if not create_if_missing:
                    raise FileNotFoundError(self._path)

                empty: dict[str, Any] = {}
                self._validate_schema(empty)
                self._atomic_write(empty)

                new_signature = self._file_signature()
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
        with self._locked_file(exclusive=False):
            signature = self._file_signature()
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
        self._ensure_parent_dir()
        dir_path = os.path.dirname(self._path) or "."
    
        fd, temp_path = tempfile.mkstemp(
            dir=dir_path,
            prefix=".tmp_",
            suffix=".json",
        )
        os.close(fd)
    
        try:
            if os.path.exists(self._path):
                existing = os.stat(self._path, follow_symlinks=False)
                os.chmod(temp_path, existing.st_mode & 0o777)
                try:
                    os.chown(temp_path, existing.st_uid, existing.st_gid)
                except PermissionError:
                    pass
    
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=self._indent, ensure_ascii=False)
                handle.write("\n")
    
                if self._sync_mode != "none":
                    handle.flush()
                    os.fsync(handle.fileno())
    
            os.replace(temp_path, self._path)
    
            if self._sync_mode == "full":
                self._fsync_parent_dir()
    
        except Exception:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            raise

    def _fsync_parent_dir(self) -> None:
        dir_path = os.path.dirname(self._path) or "."
        dir_fd = os.open(dir_path, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _stat_signature(self, path: str) -> tuple[int, int, int, int] | None:
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
    
    def _file_signature(self) -> tuple[int, int, int, int] | None:
        return self._stat_signature(self._path)
    
    @staticmethod
    def _detach(value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return copy.deepcopy(value)
        return value
    

class _ConfigTransaction(MutableMapping[str, Any]):
    """A batch edit working copy.

    Values returned here are live within the transaction on purpose, so nested
    mutations work. The committed config receives a deep-copied snapshot on
    success, so leaked references do not keep mutating the live config after
    the transaction ends.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._data: dict[str, Any] | None = None
        self._original: dict[str, Any] | None = None
        self._lock_fp = None
        self.owner_thread_id: int | None = None
    
    def __enter__(self) -> Self:
        cfg = self._config
        cfg._lock.acquire()
    
        try:
            if cfg._active_transaction is not None:
                raise RuntimeError("Nested batch edits are not supported.")
    
            cfg._ensure_parent_dir()
            self._lock_fp = open(cfg._lockfile_path(), "a+b")
            fcntl.flock(self._lock_fp, fcntl.LOCK_EX)
    
            signature = cfg._file_signature()
    
            if signature is None:
                current: dict[str, Any] = {}
                cfg._validate_schema(current)
                cfg._atomic_write(current)
                signature = cfg._file_signature()
                if signature is None:
                    raise ConfigError("Config file disappeared immediately after create.")
                cfg._data = current
                cfg._last_signature = signature
    
            elif signature == cfg._last_signature:
                current = cfg._data
    
            else:
                current = cfg._read_json_object()
                cfg._validate_schema(current)
                cfg._data = current
                cfg._last_signature = signature
    
            self._original = current
            self._data = copy.deepcopy(current)
            self.owner_thread_id = threading.get_ident()
            cfg._active_transaction = self
            return self
    
        except Exception:
            try:
                if self._lock_fp is not None:
                    fcntl.flock(self._lock_fp, fcntl.LOCK_UN)
                    self._lock_fp.close()
            finally:
                self._lock_fp = None
                self._data = None
                self._original = None
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
                if self._data is None or self._original is None:
                    raise RuntimeError("Transaction is not active.")
    
                if self._data != self._original:
                    cfg._validate_schema(self._data)
                    cfg._atomic_write(self._data)
    
                    signature = cfg._file_signature()
                    if signature is None:
                        raise ConfigError(
                            "Config file disappeared immediately after commit."
                        )
    
                    if cfg._isolate_commits:
                        cfg._data = copy.deepcopy(self._data)
                    else:
                        cfg._data = self._data
    
                    cfg._last_signature = signature
                else:
                    cfg._data = self._original
    
            return False
    
        finally:
            try:
                if self._lock_fp is not None:
                    fcntl.flock(self._lock_fp, fcntl.LOCK_UN)
                    self._lock_fp.close()
            finally:
                self._lock_fp = None
                self._data = None
                self._original = None
                self.owner_thread_id = None
                cfg._active_transaction = None
                cfg._lock.release()

    def _require_active(self) -> dict[str, Any]:
        if self._data is None:
            raise RuntimeError("Transaction is not active.")
        return self._data

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

    def pop(self, key: str, default: Any = Config._MISSING) -> Any:
        data = self._require_active()
        if default is Config._MISSING:
            return data.pop(key)
        return data.pop(key, default)

    def popitem(self) -> tuple[str, Any]:
        return self._require_active().popitem()

    def setdefault(self, key: str, default: Any = None) -> Any:
        return self._require_active().setdefault(key, default)

    def update(self, *args: Any, **kwargs: Any) -> None:
        self._require_active().update(*args, **kwargs)

