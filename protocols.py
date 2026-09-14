from __future__ import annotations

from typing import (
    Protocol, runtime_checkable, overload,
    Any, Self, Callable, Final, 
    Literal
)
from collections.abc import (
    Mapping, Iterator,
    Sequence, MutableMapping, Iterable
)
from types import TracebackType

type JsonValue = int | float | Mapping[str, JsonValue] | Sequence[JsonValue] | str | bool | None
type JsonDict = dict[str, JsonValue]

class MISSING_TYPE:
    """Sentinel for missing default values."""
    def __repr__(self) -> str:
        return "<MISSING>"

MISSING: Final[MISSING_TYPE] = MISSING_TYPE()


@runtime_checkable
class AdminBotLike(Protocol):
    """Stub protocol for AdminBot."""

    def msg(self, text: str, parse_mode: str = "HTML") -> None: ...


@runtime_checkable
class PublicBotLike(Protocol):
    """Stub protocol for PublicBot."""

    def msg(
        self, 
        tgid: int | str | None,
        key: str,
        **kwargs: Any
    ) -> None: ... 


@runtime_checkable
class ConfigLike(Protocol):
    """Stub protocol for Config."""

    @property
    def path(self) -> str: ...

    @property
    def size(self) -> int: ...

    def close(self) -> None: ...

    def backup_now(self) -> None: ...

    def reload(self) -> bool: ...

    def edit(self) -> _ConfigTransactionLike: ...

    def mutate[_T](self, callback: Callable[[MutableMapping[str, JsonValue]], _T]) -> _T: ...

    def __enter__(self) -> _ConfigTransactionLike: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None
    ) -> Literal[False] | None: ...

    def __getitem__(self, key: str) -> Any: ...

    def __setitem__(self, key: str, value: JsonValue) -> None: ...

    def __delitem__(self, key: str) -> None: ...

    def __contains__(self, key: str) -> bool: ...

    def __len__(self) -> int: ...

    @overload
    def get(self, key: str) -> JsonValue: ...

    @overload
    def get[_TJ: JsonValue](self, key: str, default: _TJ) -> _TJ: ...

    @overload
    def get[_T](self, key: str, *, as_type: type[_T]) -> _T: ...

    @overload
    def get[_T](self, key: str, default: MISSING_TYPE, *, as_type: type[_T]) -> _T: ...

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
    ) -> Any: ...
    
    def __iter__(self) -> Iterator[str]: ...

    def keys(self) -> tuple[str, ...]: ...

    def values(self) -> tuple[JsonValue, ...]: ...

    def items(self) -> tuple[tuple[str, JsonValue], ...]: ...

    def copy(self) -> dict[str, Any]: ...
    
    def clear(self) -> None: ...

    @overload
    def pop(self, key: str) -> JsonValue: ...

    @overload
    def pop[_TJ: JsonValue](self, key: str, default: _TJ) -> _TJ: ...

    def pop[_TJ: JsonValue](self, key: str, default: _TJ | MISSING_TYPE = MISSING) -> JsonValue | _TJ: ...

    def popitem(self) -> tuple[str, JsonValue]: ...

    @overload
    def setdefault[_TJ: JsonValue](self, key: str, default: _TJ) -> _TJ: ...

    @overload
    def setdefault(self, key: str, default: None = None) -> JsonValue: ...

    def setdefault(self, key: str, default: JsonValue = None) -> JsonValue: ...

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
    ) -> None: ...
    

@runtime_checkable
class _ConfigTransactionLike(Protocol):
    
    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None
    ) -> Literal[False] | None: ...

    def __getitem__(self, key: str) -> Any: ...

    def __setitem__(self, key: str, value: JsonValue) -> None: ...

    def __delitem__(self, key: str) -> None: ...

    def __iter__(self) -> Iterator[str]: ...

    def __len__(self) -> int: ...

    def __contains__(self, key: str) -> bool: ...

    @overload
    def get(self, key: str) -> JsonValue: ...

    @overload
    def get[_TJ: JsonValue](self, key: str, default: _TJ) -> _TJ: ...

    @overload
    def get[_T](self, key: str, *, as_type: type[_T]) -> _T: ...

    @overload
    def get[_T](self, key: str, default: MISSING_TYPE, *, as_type: type[_T]) -> _T: ...

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
    ) -> Any: ...

    def copy(self) -> dict[str, Any]: ...

    def clear(self) -> None: ...

    @overload
    def pop(self, key: str) -> JsonValue: ...

    @overload
    def pop[_TJ: JsonValue](self, key: str, default: _TJ) -> _TJ: ...

    def pop[_TJ: JsonValue](self, key: str, default: _TJ | MISSING_TYPE = MISSING) -> JsonValue | _TJ: ...

    def popitem(self) -> tuple[str, JsonValue]: ...

    @overload
    def setdefault[_TJ: JsonValue](self, key: str, default: _TJ) -> _TJ: ...

    @overload
    def setdefault(self, key: str, default: None = None) -> JsonValue: ...

    def setdefault(self, key: str, default: JsonValue = None) -> JsonValue: ...

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
    ) -> None: ...

@runtime_checkable
class LinesConfigLike(Protocol):
    """Stub protocol for LinesConfig."""

    @property
    def path(self) -> str: ...

    @property
    def size(self) -> int: ...

    def append(
        self,
        record: Mapping[str, JsonValue]
    ) -> None: ...

    def append_many(
        self, 
        records: Sequence[Mapping[str, JsonValue]]
    ) -> None: ...

    def __iter__(self) -> Iterator[JsonDict]: ...

    def tail(self, n: int = 100) -> Iterator[JsonDict]: ...

    def read_all(self) -> list[JsonDict]: ...

    def first(self, n: int = 1) -> list[JsonDict]: ...

    def count(self) -> int: ...

    def clear(self) -> None: ...

    def compact(self, keep: Callable[[Mapping[str, JsonValue]], bool] | None = None) -> tuple[int, int]: ...

    def backup_now(self) -> None: ...

    def close(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_val: BaseException | None,
            exc_tb: TracebackType | None,
    ) -> Literal[False] | None: ...
    