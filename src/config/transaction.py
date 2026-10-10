from __future__ import annotations

import copy
import fcntl
import threading
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self, cast, overload

from .atomic import atomic_write_json, ensure_parent_dir, file_signature
from .constants import MISSING, MISSING_TYPE, JsonDict, JsonValue

if TYPE_CHECKING:
    import io
    from types import TracebackType

    from .core import Config

from errors import ConfigError


class ConfigTransaction[Doc = JsonDict](MutableMapping[str, JsonValue]):
    """A batch edit working copy.

    Values returned here are live within the transaction on purpose, so nested
    mutations work. The committed config receives a deep-copied snapshot on
    success, so leaked references do not keep mutating the live config after
    the transaction ends.
    """

    def __init__(self, config: Config[Doc]) -> None:
        self._config: Config[Doc] = config
        self.data: dict[str, JsonValue] | None = None
        self.original: dict[str, JsonValue] | None = None
        self._lock_fp: io.BufferedRandom | None = None
        self.owner_thread_id: int | None = None
        self._cfg_lock_acquired: bool = False

    def __enter__(self) -> Self:
        cfg: Config[Doc] = self._config
        cfg.lock.acquire()
        self._cfg_lock_acquired = True
        lock_fp: io.BufferedRandom | None = None

        try:
            if cfg.active_transaction is not None:
                raise RuntimeError("Nested batch edits are not supported.")

            ensure_parent_dir(cfg.path)
            ensure_parent_dir(cfg.lockfile_path)
            lock_fp = Path(cfg.lockfile_path).open("a+b")
            fcntl.flock(lock_fp, fcntl.LOCK_EX)

            signature = file_signature(cfg.path)

            if signature is None:
                current: dict[str, JsonValue] = {}
                cfg.validate_schema(current)
                signature = atomic_write_json(
                    cfg.path, current,
                    indent=cfg.indent, minify=cfg.minify, sync_mode=cfg.sync_mode,
                )
                if signature is None:
                    raise ConfigError("Config file disappeared immediately after create.")
                cfg.data = current
                cfg.last_signature = signature

            elif signature == cfg.last_signature:
                current = copy.deepcopy(cfg.data)

            else:
                current = cfg.read_json_object()
                cfg.validate_schema(current)
                cfg.data = current
                cfg.last_signature = signature

            self.original = copy.deepcopy(current)
            self.data = copy.deepcopy(current)
            self.owner_thread_id = threading.get_ident()
            cfg.active_transaction = self
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
                cfg.active_transaction = None
                cfg.lock.release()
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
                    cfg.validate_schema(self.data)
                    cfg.atomic_write(self.data)

                    signature = file_signature(cfg.path)
                    if signature is None:
                        raise ConfigError(
                            "Config file disappeared immediately after commit."
                        )

                    if cfg.isolate_commits:
                        cfg.data = copy.deepcopy(self.data)
                    else:
                        cfg.data = self.data

                    cfg.last_signature = signature
                else:
                    cfg.data = self.original

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
                cfg.active_transaction = None
                cfg.context_transaction = None
                cfg.lock.release()
                self._cfg_lock_acquired = False

    def require_active(self) -> JsonDict:
        if self.data is None:
            raise RuntimeError("Transaction is not active.")
        return self.data

    def __getitem__(self, key: str) -> JsonValue:
        return self.require_active()[key]

    def __setitem__(self, key: str, value: JsonValue) -> None:
        self.require_active()[key] = value

    def __delitem__(self, key: str) -> None:
        del self.require_active()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.require_active())

    def __len__(self) -> int:
        return len(self.require_active())

    def __contains__(self, key: str) -> bool:
        return key in self.require_active()

    @overload
    def get(self, key: str) -> JsonValue: ...

    @overload
    def get[T](self, key: str, default: T) -> JsonValue | T: ...

    def get[T](
        self,
        key: str,
        default: T | MISSING_TYPE = MISSING,
    ) -> JsonValue | T:
        data = self.require_active()
        return data.get(key) if default is MISSING else data.get(key, cast(T, default))

    def view(self) -> Doc:
        """Return the active working document; nested edits are live."""
        return cast(Doc, self.require_active())

    def copy(self) -> JsonDict:
        return copy.deepcopy(self.require_active())

    def clear(self) -> None:
        self.require_active().clear()

    @overload
    def pop(self, key: str) -> JsonValue: ...

    @overload
    def pop[TJ: JsonValue](self, key: str, default: TJ) -> JsonValue | TJ: ...

    def pop[TJ: JsonValue](self, key: str, default: TJ | MISSING_TYPE = MISSING) -> JsonValue | TJ:
        data = self.require_active()
        if default is MISSING:
            return data.pop(key)
        d: JsonValue = default  # type: ignore[assignment]
        return data.pop(key, d)

    def popitem(self) -> tuple[str, JsonValue]:
        return self.require_active().popitem()

    @overload
    def setdefault[TJ: JsonValue](self, key: str, default: TJ) -> JsonValue | TJ: ...

    @overload
    def setdefault(self, key: str, default: None = None) -> JsonValue: ...

    def setdefault(self, key: str, default: JsonValue = None) -> JsonValue:
        return self.require_active().setdefault(key, default)

    @overload
    def update(self, **kwargs: JsonValue) -> None: ...

    @overload
    def update(self, m: Mapping[str, JsonValue], /, **kwargs: JsonValue) -> None: ...

    @overload
    def update(self, m: Iterable[tuple[str, JsonValue]], /, **kwargs: JsonValue) -> None: ...

    # whatever lol
    def update( # pyright: ignore[reportInconsistentOverload]
        self,
        m: Mapping[str, JsonValue] | Iterable[tuple[str, JsonValue]] | None = None,
        /,
        **kwargs: JsonValue
    ) -> None:
        data = self.require_active()

        if m is not None:
            data.update(m, **kwargs)
        else:
            data.update(**kwargs)
