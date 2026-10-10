from __future__ import annotations

from typing import Protocol, runtime_checkable, Self, Literal, Callable
from types import TracebackType
from collections.abc import Mapping, Iterator, Sequence

from .constants import JsonValue, JsonDict


@runtime_checkable
class LinesConfigLike(Protocol):
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
