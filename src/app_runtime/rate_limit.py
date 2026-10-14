from __future__ import annotations

import secrets
import time
from typing import TYPE_CHECKING, Protocol, cast

from loggers import Logger

if TYPE_CHECKING:
    from config import AppConfig
    from core.common import Snapshot


_RATE_LIMIT_WINDOW = 60.0
_REDIS_SOCKET_TIMEOUT = 0.2
_ADMIN_AUTH_FAILURES = 10
_ADMIN_AUTH_WINDOW = 300.0
_ADMIN_AUTH_LOCKOUT = 900
_ADMIN_AUTH_SCRIPT = """
local remaining = redis.call('PTTL', KEYS[2])
if remaining > 0 then
  return math.ceil(remaining / 1000)
end
if ARGV[3] == 'valid' then
  redis.call('DEL', KEYS[1])
  return 0
end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - tonumber(ARGV[2]))
redis.call('ZADD', KEYS[1], now, ARGV[3])
redis.call('EXPIRE', KEYS[1], math.ceil(tonumber(ARGV[2])))
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[4]) then
  redis.call('SET', KEYS[2], 'locked', 'EX', ARGV[5])
  redis.call('DEL', KEYS[1])
  return tonumber(ARGV[5])
end
return -1
"""
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
        self,
        *,
        keys: tuple[str, ...],
        args: tuple[float, float, str, int, int],
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
        self._admin_auth_script: RateLimitScript | None = None

    @classmethod
    def from_config(cls, cfg: Snapshot[AppConfig]) -> AsyncRateLimiter:
        section = cfg.view()["redis"]
        return cls(
            section["url"],
            socket_timeout=section.get("socket_timeout", _REDIS_SOCKET_TIMEOUT),
        )

    async def start(self) -> None:
        if self._client is None:
            from redis.asyncio import Redis

            self._client = cast(
                "RateLimitClient",
                Redis.from_url(  # pyright: ignore[reportUnknownMemberType]  # Upstream kwargs are untyped.
                    self.url,
                    socket_connect_timeout=self.socket_timeout,
                    socket_timeout=self.socket_timeout,
                    decode_responses=True,
                ),
            )
        await self._client.ping()
        self._script = self._client.register_script(_RATE_LIMIT_SCRIPT)
        self._admin_auth_script = self._client.register_script(_ADMIN_AUTH_SCRIPT)

    async def admin_auth(self, key: str, *, valid: bool) -> int:
        """Return 0 if authenticated, -1 on failure, or lockout seconds remaining.

        All admin routes share the IP bucket. Redis serializes checks, failure
        accounting and success resets; blocked attempts never refresh the TTL.
        """
        if self._admin_auth_script is None:
            raise RuntimeError("Redis limiter has not started")
        prefix = f"admin-auth:{{{key}}}"
        return int(await self._admin_auth_script(
            keys=(f"{prefix}:failures", f"{prefix}:lockout"),
            args=(
                0.0,
                _ADMIN_AUTH_WINDOW,
                "valid" if valid else secrets.token_hex(16),
                _ADMIN_AUTH_FAILURES,
                _ADMIN_AUTH_LOCKOUT,
            ),
        ))

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
                ),
            )
            == 1
        )

    async def close(self) -> None:
        self._script = None
        self._admin_auth_script = None
        if self._owned and self._client is not None:
            await self._client.aclose()
            self._client = None
