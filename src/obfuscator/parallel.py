from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from concurrent.futures import ProcessPoolExecutor

__all__: list[str] = ["default_jobs", "ordered_map"]


def default_jobs() -> int:
    """Use half the CPUs available to this process, with at least one worker."""
    return max(1, (os.process_cpu_count() or os.cpu_count() or 1) // 2)


def ordered_map[T, R](
    executor: ProcessPoolExecutor | None,
    function: Callable[[T], R],
    values: Iterable[T],
    *,
    chunksize: int = 8,
) -> list[R]:
    """Keep input order and propagate worker failures in serial and parallel mode."""
    if executor is None:
        return list(map(function, values))
    return list(executor.map(function, values, chunksize=chunksize, buffersize=64))
