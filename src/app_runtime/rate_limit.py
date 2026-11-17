from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from loggers import Logger

if TYPE_CHECKING:
    from config import AppConfig
    from core.common import Snapshot


_RATE_LIMIT_WINDOW = 60.0
_REDIS_SOCKET_TIMEOUT = 0.2
_REDIS_CHECK_INTERVAL = 5.0
_REDIS_MAX_CONNECTIONS = 256
_LOCAL_MAX_BUCKETS = 8192
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
local increment = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local limit = tonumber(ARGV[4])
local count = redis.call('INCRBY', KEYS[1], increment)
if count == increment then
  redis.call('EXPIRE', KEYS[1], ttl)
end
if count > limit then
  return -math.max(1, redis.call('TTL', KEYS[1]))
end
return count
"""

log = Logger("rate_limit")


def _monotonic() -> float:
    return time.monotonic()


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


@dataclass(slots=True)
class _LocalBucket:
    window: int
    count: int = 0
    pending: int = 0
    last_remote_check: float = 0.0
    remote_blocked_until: float = 0.0


class AsyncRateLimiter:
    """Runtime-owned Redis client with a local-first request limiter."""

    def __init__(
        self,
        url: str,
        *,
        socket_timeout: float = _REDIS_SOCKET_TIMEOUT,
        redis_check_interval: float = _REDIS_CHECK_INTERVAL,
        client: RateLimitClient | None = None,
    ) -> None:
        if not url or socket_timeout <= 0 or redis_check_interval <= 0:
            raise ValueError("Redis URL and positive Redis timing values are required")
        self.url = url
        self.socket_timeout = socket_timeout
        self.redis_check_interval = redis_check_interval
        self._client = client
        self._owned = client is None
        self._script: RateLimitScript | None = None
        self._admin_auth_script: RateLimitScript | None = None
        self._local: dict[tuple[str, str, int], _LocalBucket] = {}
        self._remote_lock = asyncio.Lock()

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
                RateLimitClient,
                Redis.from_url(  # pyright: ignore[reportUnknownMemberType]  # Upstream kwargs are untyped.
                    self.url,
                    socket_connect_timeout=self.socket_timeout,
                    socket_timeout=self.socket_timeout,
                    max_connections=_REDIS_MAX_CONNECTIONS,
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
        return int(
            await self._admin_auth_script(
                keys=(f"{prefix}:failures", f"{prefix}:lockout"),
                args=(
                    0.0,
                    _ADMIN_AUTH_WINDOW,
                    "valid" if valid else secrets.token_hex(16),
                    _ADMIN_AUTH_FAILURES,
                    _ADMIN_AUTH_LOCKOUT,
                ),
            )
        )

    async def allow(self, scope: str, key: str, max_requests: int) -> bool:
        return await self.retry_after(scope, key, max_requests) == 0

    async def retry_after(self, scope: str, key: str, max_requests: int) -> int:
        """Enforce a local fixed window and periodically reconcile it with Redis."""
        if max_requests <= 0:
            raise ValueError("max_requests must be positive")

        now = _monotonic()
        window = int(now // _RATE_LIMIT_WINDOW)
        local_key = (scope, key, max_requests)
        bucket = self._local.get(local_key)
        if bucket is None or bucket.window != window:
            bucket = _LocalBucket(window=window, last_remote_check=now)
            self._local[local_key] = bucket
            self._prune_local(window)

        if bucket.remote_blocked_until > now:
            return max(1, int(bucket.remote_blocked_until - now + 0.999999))

        window_end = (window + 1) * _RATE_LIMIT_WINDOW
        if bucket.count >= max_requests:
            return max(1, int(window_end - now + 0.999999))

        bucket.count += 1
        bucket.pending += 1
        if now - bucket.last_remote_check < self.redis_check_interval or self._script is None:
            return 0

        bucket.last_remote_check = now
        increment = bucket.pending
        result = await self._remote_increment(
            scope,
            key,
            bucket.window,
            increment,
            max_requests,
        )
        if result is None:
            return 0

        bucket.pending = max(0, bucket.pending - increment)
        if result <= 0:
            bucket.remote_blocked_until = now + max(1, -result)
            return max(1, int(bucket.remote_blocked_until - _monotonic() + 0.999999))
        return 0

    async def _remote_increment(
        self,
        scope: str,
        key: str,
        window: int,
        increment: int,
        max_requests: int,
    ) -> int | None:
        if self._script is None:
            return None
        try:
            acquired = await asyncio.wait_for(
                self._remote_lock.acquire(),
                timeout=self.socket_timeout,
            )
        except Exception:
            return None
        try:
            result = await asyncio.wait_for(
                self._script(
                    keys=(self._redis_key(scope, key, window),),
                    args=(
                        increment,
                        int(_RATE_LIMIT_WINDOW),
                        "",
                        max_requests,
                        int(_RATE_LIMIT_WINDOW),
                    ),
                ),
                timeout=self.socket_timeout,
            )
            return int(result)
        except Exception:
            return None
        finally:
            if acquired:
                self._remote_lock.release()

    @staticmethod
    def _redis_key(scope: str, key: str, window: int) -> str:
        digest = hashlib.sha256(f"{scope}\0{key}".encode()).hexdigest()
        return f"ratelimit:{digest}:{window}"

    def _prune_local(self, current_window: int) -> None:
        if len(self._local) <= _LOCAL_MAX_BUCKETS:
            return
        expired = [key for key, bucket in self._local.items() if bucket.window != current_window]
        for key in expired:
            self._local.pop(key, None)
        if len(self._local) <= _LOCAL_MAX_BUCKETS:
            return
        oldest = sorted(self._local, key=lambda key: self._local[key].window)
        for key in oldest[: len(self._local) - _LOCAL_MAX_BUCKETS]:
            self._local.pop(key, None)

    async def close(self) -> None:
        self._script = None
        self._admin_auth_script = None
        self._local.clear()
        if self._owned and self._client is not None:
            await self._client.aclose()
            self._client = None
