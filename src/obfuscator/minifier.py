from __future__ import annotations

from types import FunctionType
from typing import TYPE_CHECKING, Self, cast

import python_minifier
from python_minifier.module_printer import ModulePrinter
from python_minifier.token_printer import TokenPrinter

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__: list[str] = ["minify"]


class _PreservedNames(list[str]):  # pyright: ignore[reportUnusedClass] # ruff: ignore[subclass-builtin] - Retained for regression tests; the minifier expects a list.
    """Ordered minifier preserve list with constant-time membership checks.

    The pinned minifier extends these lists before checking every binding.
    Preserve list iteration and insertion order while indexing those lookups.
    """

    def __init__(self, names: Iterable[str]) -> None:
        super().__init__(names)
        self._names: set[str] = set(self)

    def __contains__(self, name: object) -> bool:
        return name in self._names

    def extend(self, names: Iterable[str]) -> None:
        incoming = list(names)
        super().extend(incoming)
        self._names.update(incoming)


class _Fragments:  # ruff: ignore[eq-without-hash] - Mutable buffer is intentionally unhashable.
    """The append, trailing trim, and last-character operations used by TokenPrinter."""

    def __init__(self) -> None:
        self.parts: list[str] = []

    def __iadd__(self, text: str) -> Self:
        if text:
            self.parts.append(text)
        return self

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str) and not other:
            return not self.parts
        return str(self) == other

    def __getitem__(self, index: int) -> str:
        if index != -1:
            raise IndexError(index)
        return self.parts[-1][-1]

    def rstrip(self, characters: str) -> Self:
        while self.parts:
            last = self.parts[-1].rstrip(characters)
            if last:
                self.parts[-1] = last
                break
            self.parts.pop()
        return self

    def __str__(self) -> str:
        return "".join(self.parts)


class _BufferedTokenPrinter(TokenPrinter):
    def __init__(self, *, prefer_single_line: bool = False) -> None:
        super().__init__(prefer_single_line=prefer_single_line)  # type: ignore[no-untyped-call]
        # Implements the operations the pinned printer performs on its buffer.
        self._code = _Fragments()  # type: ignore[assignment]

    def __str__(self) -> str:
        return str(self._code)


class _BufferedModulePrinter(ModulePrinter):
    def __init__(self, indent_char: str = "\t", prefer_single_line: bool = False) -> None:
        super().__init__(indent_char=indent_char, prefer_single_line=prefer_single_line)  # type: ignore[no-untyped-call]
        self.printer = _BufferedTokenPrinter(prefer_single_line=prefer_single_line)


def _with_globals(function: FunctionType, replacements: dict[str, object]) -> FunctionType:
    """Bind one dependency function to an isolated globals dictionary."""
    namespace = {**function.__globals__, **replacements}
    result = FunctionType(
        function.__code__, namespace, function.__name__, function.__defaults__, function.__closure__
    )
    result.__kwdefaults__ = function.__kwdefaults__
    return result


def minify(source: str, **options: object) -> str:
    """Use the pinned minifier unchanged, except for its quadratic token buffer.

    Its printer repeatedly concatenates to one growing string. Isolated function
    globals select a buffered printer without monkey-patching the dependency or
    affecting other threads. Its normal AST comparison and validation still run.
    """
    unparse = _with_globals(
        cast(FunctionType, python_minifier.unparse), {"ModulePrinter": _BufferedModulePrinter}
    )
    minifier = _with_globals(cast(FunctionType, python_minifier.minify), {"unparse": unparse})
    return cast(str, minifier(source, **options))
