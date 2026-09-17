import io
import fcntl
import copy
import threading

from collections.abc import Mapping, MutableMapping, Iterator, Iterable
from types import TracebackType
from typing import overload, Self, TYPE_CHECKING, Any, Literal, cast

from ._constants import JsonValue, JsonDict
from ._protocols import MISSING, MISSING_TYPE, MissingValue
from ._atomic import _file_signature, _atomic_write_json, _ensure_parent_dir, _lockfile_path
if TYPE_CHECKING:
    from ._core import Config
    
from errors import ConfigError


class _ConfigTransaction(MutableMapping[str, JsonValue]): # pyright: ignore[reportUnusedClass]
    """A batch edit working copy.

    Values returned here are live within the transaction on purpose, so nested
    mutations work. The committed config receives a deep-copied snapshot on
    success, so leaked references do not keep mutating the live config after
    the transaction ends.
    """

    def __init__(self, config: Config) -> None:
        self._config: Config = config
        self.data: dict[str, JsonValue] | None = None
        self.original: dict[str, JsonValue] | None = None
        self._lock_fp: io.BufferedRandom | None = None
        self.owner_thread_id: int | None = None
        self._cfg_lock_acquired: bool = False

    def __enter__(self) -> Self:
        cfg: Config = self._config
        cfg._lock.acquire()
        self._cfg_lock_acquired = True
        lock_fp: io.BufferedRandom | None = None

        try:
            if cfg._active_transaction is not None:
                raise RuntimeError("Nested batch edits are not supported.")

            _ensure_parent_dir(cfg._path)
            lock_fp = open(_lockfile_path(cfg._path), "a+b")
            fcntl.flock(lock_fp, fcntl.LOCK_EX)

            signature = _file_signature(cfg._path)

            if signature is None:
                current: dict[str, JsonValue] = {}
                cfg._validate_schema(current)
                signature = _atomic_write_json(
                    cfg._path, current,
                    indent=cfg._indent, minify=cfg._minify, sync_mode=cfg._sync_mode,
                )
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
    ) -> Literal[False] | None:
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
                cfg._context_transaction = None
                cfg._lock.release()
                self._cfg_lock_acquired = False

    def _require_active(self) -> JsonDict:
        if self.data is None:
            raise RuntimeError("Transaction is not active.")
        return self.data

    def __getitem__(self, key: str) -> Any:
        return self._require_active()[key]

    def __setitem__(self, key: str, value: JsonValue) -> None:
        self._require_active()[key] = value

    def __delitem__(self, key: str) -> None:
        del self._require_active()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._require_active())

    def __len__(self) -> int:
        return len(self._require_active())

    def __contains__(self, key: str) -> bool:
        return key in self._require_active()

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
    def get[_T, _TJ: JsonValue](self, key: str, default: _TJ | MISSING_TYPE = MISSING, *, as_type: type[_T] | None = None) -> _TJ | _T: ...

    def get[_T](
        self,
        key: str,
        default: JsonValue | MISSING_TYPE = MISSING,
        *,
        as_type: type[_T] | None = None
    ) -> Any:
        data = self._require_active()
        value = data.get(key) if default is MISSING else data.get(key, default)
        if as_type is not None:
            value = cast(as_type, value)  # type: ignore[valid-type]
        return value # pyright: ignore[reportUnknownVariableType]

    def copy(self) -> dict[str, Any]:
        return copy.deepcopy(self._require_active())

    def clear(self) -> None:
        self._require_active().clear()

    @overload
    def pop(self, key: str) -> JsonValue: ...

    @overload
    def pop[_TJ: JsonValue](self, key: str, default: _TJ) -> _TJ: ...

    def pop[_TJ: JsonValue](self, key: str, default: _TJ | MISSING_TYPE = MISSING) -> JsonValue | _TJ:
        data = self._require_active()
        if default is MISSING:
            return data.pop(key)
        d: JsonValue = default  # type: ignore[assignment]
        return data.pop(key, d)

    def popitem(self) -> tuple[str, JsonValue]:
        return self._require_active().popitem()

    @overload
    def setdefault[_TJ: JsonValue](self, key: str, default: _TJ) -> _TJ: ...

    @overload
    def setdefault(self, key: str, default: None = None) -> JsonValue: ...

    def setdefault(self, key: str, default: JsonValue = None) -> JsonValue:
        return self._require_active().setdefault(key, default)

    @overload
    def update(self, **kwargs: JsonValue) -> None: ...

    @overload
    def update(self, __m: Mapping[str, JsonValue], **kwargs: JsonValue) -> None: ...

    @overload
    def update(self, __m: Iterable[tuple[str, JsonValue]], **kwargs: JsonValue) -> None: ...

    # whatever lol
    def update( # pyright: ignore[reportInconsistentOverload]
        self,
        __m: Mapping[str, JsonValue] | Iterable[tuple[str, JsonValue]] | None = None,
        **kwargs: JsonValue
    ) -> None:
        data = self._require_active()

        if __m is not None:
            data.update(__m, **kwargs)
        else:
            data.update(**kwargs)

