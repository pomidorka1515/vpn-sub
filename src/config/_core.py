import threading
import os
import copy

from collections.abc import Mapping, MutableMapping, Iterator, Iterable
from pathlib import Path
from types import TracebackType
from typing import Any, overload, cast, Callable, Literal

from ._constants import JsonValue, SYNC_MODES
from ._protocols import MISSING, MISSING_TYPE, MissingValue
from ._protocols import _ConfigTransactionLike
from ._atomic import FileSignature, _file_signature, _locked_file, _atomic_write_json
from ._backup import _prune_backups, _do_backup, _make_backup_thread, _instance_backup_dir
from ._schema import _load_schema, _validate_schema, _read_json_object
from ._transaction import _ConfigTransaction

from errors import ConfigError, ReadOnlyConfigError
from loggers import Logger

class Config(MutableMapping[str, JsonValue]):
    """
    Thread-safe, process-safe JSON config manager.

    Notes:
    - Uses a dedicated `.lock` file for inter-process locking.
    - Uses atomic replace for writes.
    - `with cfg as tx:` returns a transaction object backed by a working copy.
    - Top-level JSON must be an object.
    """

    def __init__(
        self,
        *,
        path: str | Path,
        indent: int = 4,
        minify: bool = False,
        read_only: bool = False,
        read_only_jsonc: bool = False,
        strict_schema: bool = True,
        sync_mode: SYNC_MODES = 'data',
        isolate_commits: bool = True,
        backup_dir: str | Path | None = None,
        backup_interval: int | float = 7200,
        backup_retention: int = 3
    ) -> None:
        """
        Args:
            path: Path to the JSON config file (created if missing).
            indent: JSON indentation width for writes.
            minify: If True, minify JSON instead of beautifying it.
                indent param is skipped if this is set.
            read_only: Read-only, raises on writes. Handle with caution.
                WARNING: This arg is also immutable.
                You cannot make a Config() instance read-only past __init__ and vise versa.
            read_only_jsonc: Parse JSONC comments and trailing commas. This is only
                supported for read-only configs so the source file is never reformatted.
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
        path_str = str(path)

        valid_exts: tuple[str] | tuple[str, str] = ('.jsonc', '.json') if read_only else ('.json',)
        if read_only_jsonc and not read_only:
            raise ConfigError("read_only_jsonc requires read_only=True")
        if not path_str.endswith(valid_exts):
            self.log.warning(f"path doesnt end with .json{"c" if read_only else ""}, did you specify the correct path?")
        self._path: str = path_str
        self._indent: int = indent
        self._minify: bool = minify
        self._strict_schema: bool = strict_schema

        if sync_mode not in ("full", "data", "none"):
            raise ValueError("sync_mode must be 'full', 'data', or 'none'")
        self._sync_mode: SYNC_MODES = sync_mode

        # True = safer: committed state is detached from leaked tx refs
        # False = faster but unsafe if refs escape the transaction
        self._isolate_commits: bool = isolate_commits

        self._data: dict[str, JsonValue] = {}
        self._last_signature: FileSignature | None = None

        self._lock = threading.RLock()
        self._active_transaction: _ConfigTransaction | None = None
        self._context_transaction: _ConfigTransaction | None = None

        self._schema_cache_path: str | None = None
        self._schema_cache_signature: FileSignature | None = None
        self._schema_cache: dict[str, JsonValue] | None = None

        self._warned_update_callable: bool = False

        self._read_only: bool = read_only
        self._read_only_jsonc: bool = read_only_jsonc or (path_str.endswith('.jsonc') and read_only)

        self._backup_dir: str | None = str(backup_dir) if backup_dir else None
        self._backup_interval: int | float = backup_interval
        self._backup_retention: int = backup_retention
        self._backup_stop = threading.Event()

        with self.log.loading():
            self.reload()

        if self._backup_dir:
            self._backup_t: threading.Thread | None = _make_backup_thread(
                path=self._path,
                indent=self._indent,
                backup_dir=self._backup_dir,
                backup_interval=backup_interval,
                backup_retention=backup_retention,
                stop_event=self._backup_stop,
                config_type='json',
                jsonc=self._read_only_jsonc,
            )
            self._backup_t.start()
        else:
            self._backup_t = None

    # properties for the sake of immutability

    @property
    def path(self) -> str:
        return self._path

    @property
    def size(self) -> int:
        return os.path.getsize(self.path)

    @property
    def indent(self) -> int:
        return self._indent

    @property
    def minify(self) -> bool:
        return self._minify

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def read_only_jsonc(self) -> bool:
        return self._read_only_jsonc

    @property
    def strict_schema(self) -> bool:
        return self._strict_schema

    @property
    def sync_mode(self) -> SYNC_MODES:
        return self._sync_mode

    @property
    def isolate_commits(self) -> bool:
        return self._isolate_commits

    @property
    def backup_dir(self) -> str | None:
        return self._backup_dir

    @property
    def backup_interval(self) -> int | float:
        return self._backup_interval

    @property
    def backup_retention(self) -> int:
        return self._backup_retention

    def close(self) -> None:
        """Stop the backup thread if one is running. Does not affect the data file."""
        self._backup_stop.set()
        if self._backup_t is not None:
            self._backup_t.join(timeout=5)

    def backup_now(self) -> None:
        """Trigger an immediate backup snapshot, regardless of the schedule.

        Obeys the same retention limit as the scheduled backup thread.

        Raises:
            ConfigError: If no backup_dir was configured on this instance.
        """
        if self._backup_dir is None:
            raise ConfigError("backup_now() requires a backup_dir to be configured.")
        instance_dir = _instance_backup_dir(self._path, self._backup_dir)
        _do_backup(
            self._path,
            self._indent,
            instance_dir,
            self.log,
            minify=self._minify,
            jsonc=self._read_only_jsonc,
        )
        _prune_backups(instance_dir, self._backup_retention, self.log, config_type='json')

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
            return self._reload_locked(
                create_if_missing=not self._read_only_jsonc,
                exclusive=True,
            )

    def edit(self) -> _ConfigTransactionLike:
        """Open an explicit transaction.

        Usage:
            with cfg.edit() as tx:
                tx["x"].pop("124", None)
                tx["count"] += 1
        """
        self._raise_if_read_only()
        return cast(_ConfigTransactionLike, _ConfigTransaction(self))

    def mutate[_T](self, callback: Callable[[MutableMapping[str, JsonValue]], _T]) -> _T:
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
            result = callback(cast(_ConfigTransaction, tx))
        return self._detach(result)

    def __enter__(self) -> _ConfigTransactionLike:
        self._raise_if_read_only()
        tx = self.edit()
        self._context_transaction = cast(_ConfigTransaction, tx)
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
    ) -> Literal[False] | None:
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

    def __setitem__(self, key: str, value: JsonValue) -> None:
        self._raise_if_read_only()
        self._run_edit(lambda tx: tx.__setitem__(key, value))

    def __delitem__(self, key: str) -> None:
        self._raise_if_read_only()
        self._run_edit(lambda tx: tx.__delitem__(key))

    def __contains__(self, key: str) -> bool:
        with self._lock:
            self._raise_if_used_inside_transaction()
            self._ensure_recent_locked()
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            self._raise_if_used_inside_transaction()
            self._ensure_recent_locked()
            return len(self._data)

    @overload
    def get(self, key: str) -> JsonValue: ...

    @overload
    def get[_TJ: JsonValue](self, key: str, default: _TJ) -> _TJ: ...

    @overload
    def get[_T](self, key: str, *, as_type: type[_T]) -> _T: ...

    @overload
    def get[_T](self, key: str, default: MissingValue, *, as_type: type[_T]) -> _T: ...

    @overload
    def get[_T, _TJ: JsonValue](self, key: str, default: _TJ, *, as_type: type[_T]) -> _TJ | _T: ...

    @overload
    def get[_T, _TJ: JsonValue](
        self,
        key: str,
        default: _TJ | MISSING_TYPE = MISSING,
        *,
        as_type: type[_T] | None = None
    ) -> _TJ | _T: ...

    def get[_T](
        self,
        key: str,
        default: JsonValue | MISSING_TYPE = MISSING,
        *,
        as_type: type[_T] | None = None
    ) -> Any:
        with self._lock:
            self._raise_if_used_inside_transaction()
            self._ensure_recent_locked()
            value = self._data.get(key) if default is MISSING else self._data.get(key, default)
            if as_type is not None:
                value = cast(as_type, value)  # type: ignore[valid-type]
            return self._detach(value) # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            self._raise_if_used_inside_transaction()
            self._ensure_recent_locked()
            return iter(tuple(self._data.keys()))

    def keys(self) -> tuple[str, ...]:
        with self._lock:
            self._raise_if_used_inside_transaction()
            self._ensure_recent_locked()
            return tuple(self._data.keys())

    def values(self) -> tuple[JsonValue, ...]:
        with self._lock:
            self._raise_if_used_inside_transaction()
            self._ensure_recent_locked()
            return tuple(self._detach(v) for v in self._data.values())

    def items(self) -> tuple[tuple[str, JsonValue], ...]:
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

    @overload
    def pop(self, key: str) -> JsonValue: ...

    @overload
    def pop[_TJ: JsonValue](self, key: str, default: _TJ) -> _TJ: ...

    def pop[_TJ: JsonValue](self, key: str, default: _TJ | MISSING_TYPE = MISSING) -> JsonValue | _TJ:
        self._raise_if_read_only()
        def action(tx: _ConfigTransaction) -> JsonValue:
            if default is MISSING:
                return tx.pop(key)
            d: JsonValue = default  # type: ignore[assignment]
            return tx.pop(key, d)
        return self._run_edit(action)

    def popitem(self) -> tuple[str, JsonValue]:
        self._raise_if_read_only()
        return self._run_edit(lambda tx: tx.popitem())

    @overload
    def setdefault[_TJ: JsonValue](self, key: str, default: _TJ) -> _TJ: ...

    @overload
    def setdefault(self, key: str, default: None = None) -> JsonValue: ...

    def setdefault(self, key: str, default: JsonValue = None) -> JsonValue:
        self._raise_if_read_only()
        return self._run_edit(lambda tx: tx.setdefault(key, default))

    @overload
    def update(self, **kwargs: JsonValue) -> None: ...

    @overload
    def update(self, __m: Mapping[str, JsonValue], **kwargs: JsonValue) -> None: ...

    @overload
    def update(self, __m: Iterable[tuple[str, JsonValue]], **kwargs: JsonValue) -> None: ...

    def update( # pyright: ignore[reportInconsistentOverload]
        self,
        __m: Mapping[str, JsonValue] | Iterable[tuple[str, JsonValue]] | None = None,
        **kwargs: JsonValue
    ) -> None:
        """Atomic mapping-style update."""
        self._raise_if_read_only()

        if __m is not None:
            # dict() handles both mappings and iterables safely
            updates = dict(__m, **kwargs)
        else:
            updates = kwargs

        self._run_edit(lambda tx: tx.update(updates))

    def _run_edit[_TJ: JsonValue](self, action: Callable[[_ConfigTransaction], _TJ]) -> _TJ:
        with self.edit() as tx:
            result = action(cast(_ConfigTransaction, tx))
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

    def _read_json_object(self) -> dict[str, JsonValue]:
        return _read_json_object(self)

    def _load_schema(self, data: Mapping[str, JsonValue]) -> Mapping[str, JsonValue] | None:
        return _load_schema(self, data)

    def _validate_schema(self, data: dict[str, JsonValue]) -> None:
        return _validate_schema(self, data)

    def _reload_locked(self, *, create_if_missing: bool, exclusive: bool) -> bool:
        with _locked_file(self._path, exclusive=exclusive):
            signature = _file_signature(self._path)

            if signature is None:
                if not create_if_missing:
                    raise FileNotFoundError(self._path)

                empty: dict[str, JsonValue] = {}
                self._validate_schema(empty)
                new_signature = _atomic_write_json(
                    self._path, empty,
                    indent=self._indent, minify=self._minify, sync_mode=self._sync_mode,
                )
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

    def _atomic_write(self, data: dict[str, JsonValue]) -> None:
        self._raise_if_read_only()
        _atomic_write_json(
            self._path, data,
            indent=self._indent, minify=self._minify, sync_mode=self._sync_mode,
        )

    @staticmethod
    def _detach[_T: object](value: _T) -> _T:
        if isinstance(value, (dict, list)):
            return copy.deepcopy(cast(_T, value))
        return value
