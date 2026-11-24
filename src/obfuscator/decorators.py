from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection

__all__: list[str] = ["no_rename", "no_rename_arg"]


def no_rename[T: Callable[..., object] | type[object]](target: T) -> T:
    """Preserve a function or class name in release builds; identity at runtime."""
    return target


def no_rename_arg[**P, R](
    names: Collection[str],
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Preserve selected parameter names (or all with '*') in release builds."""
    return _identity_function


def _identity_function[**P, R](target: Callable[P, R]) -> Callable[P, R]:
    return target
