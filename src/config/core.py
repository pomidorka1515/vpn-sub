from __future__ import annotations

import threading
import os
import copy

from collections.abc import Mapping, MutableMapping, Iterator, Iterable
from pathlib import Path
from types import TracebackType
from typing import overload, cast, Literal
from collections.abc import Callable

from .constants import JsonValue, JsonDict, SYNC_MODES, MISSING, MISSING_TYPE
from .atomic import FileSignature, file_signature, locked_file, atomic_write_json, resolve_lockfile_path
from .backup import prune_backups, do_backup, make_backup_thread, instance_backup_dir
from .schema import load_schema, validate_schema, read_json_object
from .transaction import ConfigTransaction

from errors import ConfigError, ReadOnlyConfigError
from loggers import Logger

class Config[Doc = JsonDict](MutableMapping[str, JsonValue]):
    """
    Thread-safe, process-safe JSON config manager.

    The document type is a caller-supplied static contract, independent of
    runtime schema selection and validation.

    Notes:
    - Uses a dedicated lock file for inter-process locking.
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
        schema_path: str | Path | None = None,
        sync_mode: SYNC_MODES = 'data',
        isolate_commits: bool = True,
        backup_dir: str | Path | None = None,
        backup_interval: int | float = 7200,
        backup_retention: int = 3,
        lockfile_path: str | Path | None = None,
        start_backup: bool = True,
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
            schema_path: Local schema file to validate against, regardless of any
                ``$schema`` value in the JSON. If both are set and differ, a warning
                is logged and this argument wins.
            sync_mode: 'full' fsyncs file + parent directory, 'data' fsyncs file only,
                'none' skips fsync entirely.
            isolate_commits: If True, deep-copies data after commit so any references
                that leaked out of the transaction can't mutate the live config.
            backup_dir: Backup directory. Backups are disabled if set to None.
            backup_interval: Interval in seconds for the backups, in seconds.
            backup_retention: Amount of concurrent backups kept on disk.
            start_backup: Start the scheduled backup thread. False keeps
                backup_dir configured so backup_now() still works.
            lockfile_path: Inter-process lock location. None keeps it beside the
                data file as ``{path}.lock``. A directory places
                ``{basename}.{sha1(abspath)[:8]}.lock`` inside it so same-named
                files do not collide. A file path is used as-is
                (``/x/lock.file`` -> ``/x/lock.file``).
        """
        self.log = Logger(type(self).__name__)
        path_str = str(path)

        valid_exts: tuple[str] | tuple[str, str] = ('.jsonc', '.json') if read_only else ('.json',)
        if read_only_jsonc and not read_only:
            raise ConfigError("read_only_jsonc requires read_only=True")
        if not path_str.endswith(valid_exts):
            self.log.warning(f"path doesnt end with .json{"c" if read_only else ""}, did you specify the correct path?")
        self._path: str = path_str
        self._lockfile_path: str = resolve_lockfile_path(
            path_str,
            str(lockfile_path) if lockfile_path is not None else None,
        )
        self._indent: int = indent
        self._minify: bool = minify
        self._strict_schema: bool = strict_schema
        self._schema_path: str | None = os.path.normpath(str(schema_path)) if schema_path else None

        if sync_mode not in ("full", "data", "none"):
            raise ValueError("sync_mode must be 'full', 'data', or 'none'")
        self._sync_mode: SYNC_MODES = sync_mode

        # True = safer: committed state is detached from leaked tx refs
        # False = faster but unsafe if refs escape the transaction
        self._isolate_commits: bool = isolate_commits

        self.data: dict[str, JsonValue] = {}
        self.last_signature: FileSignature | None = None

        self.lock = threading.RLock()
        self.active_transaction: ConfigTransaction[Doc] | None = None
        self.context_transaction: ConfigTransaction[Doc] | None = None

        self.schema_cache_path: str | None = None
        self.schema_cache_signature: FileSignature | None = None
        self.schema_cache: dict[str, JsonValue] | None = None

        self._warned_update_callable: bool = False

        self._read_only: bool = read_only
        self._read_only_jsonc: bool = read_only_jsonc or (path_str.endswith('.jsonc') and read_only)

        self._backup_dir: str | None = str(backup_dir) if backup_dir else None
        self._backup_interval: int | float = backup_interval
        self._backup_retention: int = backup_retention
        self._backup_stop = threading.Event()

        with self.log.loading():
            self.reload()

        if self._backup_dir and start_backup:
            self._backup_t: threading.Thread | None = make_backup_thread(
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
    def lockfile_path(self) -> str:
        return self._lockfile_path

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
    def schema_path(self) -> str | None:
        return self._schema_path

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
        self.write_backup(None)

    def backup_data(self, data: Mapping[str, JsonValue]) -> None:
        """Snapshot a document that is not yet the file. Same retention as backup_now()."""
        self.write_backup(dict(data))

    def write_backup(self, data: dict[str, JsonValue] | None) -> None:
        if self._backup_dir is None:
            raise ConfigError("backup_now() requires a backup_dir to be configured.")
        instance_dir = instance_backup_dir(self._path, self._backup_dir)
        do_backup(
            self._path,
            self._indent,
            instance_dir,
            self.log,
            minify=self._minify,
            jsonc=self._read_only_jsonc,
            data=data,
        )
        prune_backups(instance_dir, self._backup_retention, self.log, config_type='json')

    def validate_document(self, data: Mapping[str, JsonValue]) -> None:
        """Schema-check a document without writing. Raises SchemaValidationError."""
        self.validate_schema(dict(data))

    def raise_if_read_only(self) -> None:
        if self._read_only:
            raise ReadOnlyConfigError("Cannot modify read-only config instance")

    def reload(self) -> bool:
        """Reload from disk if the file changed since last load. Creates the file if missing.

        Returns:
            True if the data was actually reloaded, False if the file was unchanged.
        """
        with self.lock:
            self.raise_if_used_inside_transaction()
            return self.reload_locked(
                create_if_missing=not self._read_only_jsonc,
                exclusive=True,
            )

    def edit(self) -> ConfigTransaction[Doc]:
        """Open an explicit transaction.

        Usage:
            with cfg.edit() as tx:
                tx["x"].pop("124", None)
                tx["count"] += 1
        """
        self.raise_if_read_only()
        return ConfigTransaction(self)

    def mutate[T](self, callback: Callable[[MutableMapping[str, JsonValue]], T]) -> T:
        """Run a callback inside a transaction and return its result.

        Keep callbacks short and non-blocking: they run while holding the
        in-process lock and the inter-process file lock.

        Args:
            callback: Called with the transaction mapping as its only argument.

        Returns:
            Deep-copied return value of the callback.
        """
        self.raise_if_read_only()
        with self.edit() as tx:
            result = callback(tx)
        return self.detach(result)

    def __enter__(self) -> ConfigTransaction[Doc]:
        self.raise_if_read_only()
        tx = self.edit()
        self.context_transaction = tx
        try:
            return tx.__enter__()
        except Exception:
            self.context_transaction = None
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> Literal[False] | None:
        tx = self.context_transaction
        self.context_transaction = None
        if tx is None:
            raise RuntimeError("Config.__exit__ called without a matching __enter__().")
        return tx.__exit__(exc_type, exc_val, exc_tb)

    def __getitem__(self, key: str) -> JsonValue:
        with self.lock:
            self.raise_if_used_inside_transaction()
            self.ensure_recent_locked()
            return self.detach(self.data[key])

    def __setitem__(self, key: str, value: JsonValue) -> None:
        self.raise_if_read_only()
        self.run_edit(lambda tx: tx.__setitem__(key, value))

    def __delitem__(self, key: str) -> None:
        self.raise_if_read_only()
        self.run_edit(lambda tx: tx.__delitem__(key))

    def __contains__(self, key: str) -> bool:
        with self.lock:
            self.raise_if_used_inside_transaction()
            self.ensure_recent_locked()
            return key in self.data

    def __len__(self) -> int:
        with self.lock:
            self.raise_if_used_inside_transaction()
            self.ensure_recent_locked()
            return len(self.data)

    @overload
    def get(self, key: str) -> JsonValue: ...

    @overload
    def get[T](self, key: str, default: T) -> JsonValue | T: ...

    def get[T](
        self,
        key: str,
        default: T | MISSING_TYPE = MISSING,
    ) -> JsonValue | T:
        with self.lock:
            self.raise_if_used_inside_transaction()
            self.ensure_recent_locked()
            value = self.data.get(key) if default is MISSING else self.data.get(key, cast(T, default))
            return self.detach(value)

    def __iter__(self) -> Iterator[str]:
        with self.lock:
            self.raise_if_used_inside_transaction()
            self.ensure_recent_locked()
            return iter(tuple(self.data.keys()))

    def keys(self) -> tuple[str, ...]:
        with self.lock:
            self.raise_if_used_inside_transaction()
            self.ensure_recent_locked()
            return tuple(self.data.keys())

    def values(self) -> tuple[JsonValue, ...]:
        with self.lock:
            self.raise_if_used_inside_transaction()
            self.ensure_recent_locked()
            return tuple(self.detach(v) for v in self.data.values())

    def items(self) -> tuple[tuple[str, JsonValue], ...]:
        with self.lock:
            self.raise_if_used_inside_transaction()
            self.ensure_recent_locked()
            return tuple((k, self.detach(v)) for k, v in self.data.items())

    def view(self) -> Doc:
        """Return a detached snapshot under the caller's document contract."""
        with self.lock:
            self.raise_if_used_inside_transaction()
            self.ensure_recent_locked()
            return self.detach(cast(Doc, self.data))

    def copy(self) -> JsonDict:
        """Return a deep copy of the current config data as a plain dict."""
        with self.lock:
            self.raise_if_used_inside_transaction()
            self.ensure_recent_locked()
            return copy.deepcopy(self.data)

    def clear(self) -> None:
        self.raise_if_read_only()
        self.run_edit(lambda tx: tx.clear())

    @overload
    def pop(self, key: str) -> JsonValue: ...

    @overload
    def pop[TJ: JsonValue](self, key: str, default: TJ) -> JsonValue | TJ: ...

    def pop[TJ: JsonValue](self, key: str, default: TJ | MISSING_TYPE = MISSING) -> JsonValue | TJ:
        self.raise_if_read_only()
        def action(tx: ConfigTransaction[Doc]) -> JsonValue:
            if default is MISSING:
                return tx.pop(key)
            d: JsonValue = default  # type: ignore[assignment]
            return tx.pop(key, d)
        return self.run_edit(action)

    def popitem(self) -> tuple[str, JsonValue]:
        self.raise_if_read_only()
        return self.run_edit(lambda tx: tx.popitem())

    @overload
    def setdefault[TJ: JsonValue](self, key: str, default: TJ) -> JsonValue | TJ: ...

    @overload
    def setdefault(self, key: str, default: None = None) -> JsonValue: ...

    def setdefault(self, key: str, default: JsonValue = None) -> JsonValue:
        self.raise_if_read_only()
        return self.run_edit(lambda tx: tx.setdefault(key, default))

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
        self.raise_if_read_only()

        # dict() handles both mappings and iterables safely
        updates = dict(__m, **kwargs) if __m is not None else kwargs

        self.run_edit(lambda tx: tx.update(updates))

    def run_edit[TJ: JsonValue](self, action: Callable[[ConfigTransaction[Doc]], TJ]) -> TJ:
        with self.edit() as tx:
            result = action(tx)
        return self.detach(result)

    def raise_if_used_inside_transaction(self) -> None:
        """Raise if the calling thread already owns an active transaction.

        Direct Config access (e.g. cfg["key"]) inside a transaction block would
        bypass the working copy and cause a stale-read / lost-write hazard.
        """
        tx = self.active_transaction
        if tx is None:
            return
        if tx.owner_thread_id == threading.get_ident():
            raise RuntimeError(
                "Use the transaction object returned by 'with cfg as tx' "
                "while a batch edit is active."
            )

    def read_json_object(self) -> dict[str, JsonValue]:
        return read_json_object(self)

    def load_schema(self, data: Mapping[str, JsonValue]) -> Mapping[str, JsonValue] | None:
        return load_schema(self, data)

    def validate_schema(self, data: dict[str, JsonValue]) -> None:
        return validate_schema(self, data)

    def reload_locked(self, *, create_if_missing: bool, exclusive: bool) -> bool:
        with locked_file(self._path, exclusive=exclusive, lockfile_path=self._lockfile_path):
            signature = file_signature(self._path)

            if signature is None:
                if not create_if_missing:
                    raise FileNotFoundError(self._path)

                empty: dict[str, JsonValue] = {}
                self.validate_schema(empty)
                new_signature = atomic_write_json(
                    self._path, empty,
                    indent=self._indent, minify=self._minify, sync_mode=self._sync_mode,
                )
                if new_signature is None:
                    raise ConfigError("Config file disappeared immediately after create.")

                self.data = {}
                self.last_signature = new_signature
                return True

            if signature == self.last_signature:
                return False

            data = self.read_json_object()
            self.validate_schema(data)
            self.data = data
            self.last_signature = signature
            return True

    def ensure_recent_locked(self) -> None:
        """Reload from disk under a shared lock if the file has changed.

        Called before every read to guarantee consistency in multi-process
        deployments where another worker may have committed a write.
        """
        with locked_file(self._path, exclusive=False, lockfile_path=self._lockfile_path):
            signature = file_signature(self._path)
            if signature is None:
                raise ConfigError(
                    f"Config file '{self._path}' disappeared while in use."
                )

            if signature == self.last_signature:
                return

            data = self.read_json_object()
            self.validate_schema(data)
            self.data = data
            self.last_signature = signature

    def atomic_write(self, data: dict[str, JsonValue]) -> None:
        self.raise_if_read_only()
        atomic_write_json(
            self._path, data,
            indent=self._indent, minify=self._minify, sync_mode=self._sync_mode,
        )

    @staticmethod
    def detach[T: object](value: T) -> T:
        if isinstance(value, (dict, list)):
            return copy.deepcopy(cast(T, value))
        return value
