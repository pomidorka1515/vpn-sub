from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


async def named_to_thread[**P, T](
    name: str, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs,
) -> T:
    """Label a shared executor worker for this operation, restoring it on exit."""
    def worker() -> T:
        thread = threading.current_thread()
        original_name = thread.name
        thread.name = name
        try:
            return function(*args, **kwargs)
        finally:
            thread.name = original_name

    return await asyncio.to_thread(worker)  # ruff: ignore[banned-api]  # Central named worker boundary.
