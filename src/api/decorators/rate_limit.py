from __future__ import annotations

from functools import wraps
from typing import TYPE_CHECKING, cast

from quart import request

from loggers import Logger
from util import err

from ._common import DecoratedReturn

if TYPE_CHECKING:
    from collections.abc import Callable

    from api.common import AsyncBaseApi

    from ._common import Decorated, WrappedReturn

log = Logger("rate_limit")


def rate_limit[API_T: AsyncBaseApi, **P, R](
    max_requests: int,
) -> Callable[[Decorated[API_T, P, R]], DecoratedReturn[API_T, P, R]]:
    if max_requests <= 0:
        raise ValueError("max_requests must be positive")

    def decorator(f: Decorated[API_T, P, R]) -> DecoratedReturn[API_T, P, R]:
        scope = f"{f.__module__}.{f.__qualname__}:{max_requests}"

        @wraps(f)
        async def wrapper(self: API_T, *args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]:
            try:
                retry_after = await self.limiter.retry_after(
                    scope,
                    request.remote_addr or "<unknown>",
                    max_requests,
                )
            except Exception:
                log.error("rate limit store failed")  # ruff: ignore[error-instead-of-exception] credentials may occur in exception text
                return err("Too many requests.", 429)
            if retry_after:
                response, status = err("Too many requests.", 429)
                response.headers["Retry-After"] = str(retry_after)
                return response, status
            return await f(self, *args, **kwargs)

        return cast(DecoratedReturn[API_T, P, R], wrapper)

    return decorator
