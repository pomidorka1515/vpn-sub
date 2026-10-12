from __future__ import annotations

import time
from functools import wraps
from typing import TYPE_CHECKING, Protocol, cast

from quart import request

from loggers import Logger
from util import err

if TYPE_CHECKING:
    from collections.abc import Callable

    from api.common import AsyncBaseApi
    from config import AppConfig
    from core.async_common import Snapshot

    from ._common import Decorated, DecoratedReturn, WrappedReturn

_RATE_LIMIT_WINDOW = 60.0
_REDIS_SOCKET_TIMEOUT = 0.2
_RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local member = ARGV[3]
local limit = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)
if count < limit then
  redis.call('ZADD', key, now, member)
  redis.call('EXPIRE', key, ttl)
  return 1
end
if count > 0 then
  redis.call('EXPIRE', key, ttl)
end
return 0
"""

log = Logger("rate_limit")


class RateLimitScript(Protocol):
    async def __call__(
        self, *, keys: tuple[str, ...], args: tuple[float, float, str, int, int]
    ) -> int: ...


class RateLimitClient(Protocol):
    def register_script(self, script: str) -> RateLimitScript: ...

    async def ping(self) -> bool: ...

    async def aclose(self) -> None: ...


class AsyncRateLimiter:
    """Runtime-owned Redis client; no connections or tasks at import time."""

    def __init__(
        self,
        url: str,
        *,
        socket_timeout: float = _REDIS_SOCKET_TIMEOUT,
        client: RateLimitClient | None = None,
    ) -> None:
        if not url or socket_timeout <= 0:
            raise ValueError("Redis URL and positive socket timeout are required")
        self.url = url
        self.socket_timeout = socket_timeout
        self._client = client
        self._owned = client is None
        self._script: RateLimitScript | None = None

    @classmethod
    def from_config(cls, cfg: Snapshot[AppConfig]) -> AsyncRateLimiter:
        section = cfg.view()["redis"]
        return cls(
            section["url"], socket_timeout=section.get("socket_timeout", _REDIS_SOCKET_TIMEOUT)
        )

    async def start(self) -> None:
        if self._client is None:
            from redis.asyncio import Redis

            self._client = cast(
                "RateLimitClient",
                Redis.from_url(
                    self.url,
                    socket_connect_timeout=self.socket_timeout,
                    socket_timeout=self.socket_timeout,
                    decode_responses=True,
                ),
            )  # pyright: ignore[reportUnknownMemberType]  # Upstream kwargs are untyped.
        await self._client.ping()
        self._script = self._client.register_script(_RATE_LIMIT_SCRIPT)

    async def allow(self, scope: str, key: str, max_requests: int) -> bool:
        if self._script is None:
            raise RuntimeError("Redis limiter has not started")
        now = time.time()
        return (
            int(
                await self._script(
                    keys=(f"ratelimit:{scope}:{key}",),
                    args=(
                        now,
                        _RATE_LIMIT_WINDOW,
                        f"{now}:{time.time_ns()}",
                        max_requests,
                        int(_RATE_LIMIT_WINDOW),
                    ),
                )
            )
            == 1
        )

    async def close(self) -> None:
        self._script = None
        if self._owned and self._client is not None:
            await self._client.aclose()
            self._client = None


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
                allowed = await self.limiter.allow(
                    scope, request.remote_addr or "<unknown>", max_requests
                )
            except Exception:
                log.error("rate limit store failed")  # ruff: ignore[error-instead-of-exception] credentials may occur in exception text
                return err("Too many requests.", 429)
            if not allowed:
                return err("Too many requests.", 429)
            return await f(self, *args, **kwargs)

        return cast("DecoratedReturn[API_T, P, R]", wrapper)

    return decorator
