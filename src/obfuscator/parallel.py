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
    on_result: Callable[[int], None] | None = None,
) -> list[R]:
    """Keep input order and propagate worker failures in serial and parallel mode."""
    results = (
        map(function, values)
        if executor is None
        else executor.map(function, values, chunksize=chunksize, buffersize=64)
    )
    if on_result is None:
        return list(results)
    collected: list[R] = []
    for result in results:
        collected.append(result)
        on_result(len(collected))
    return collected
