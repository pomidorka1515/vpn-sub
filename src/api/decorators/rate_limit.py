from __future__ import annotations

from ._common import Decorated, DecoratedReturn, WrappedReturn
from collections.abc import Mapping
from util import err
from flask import request
from functools import wraps
from typing import Protocol, cast, Callable
from loggers import Logger
import threading
import time

from ..common import BaseApi

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


class _RateLimitScript(Protocol):
    def __call__(
        self,
        keys: tuple[str, ...],
        args: tuple[float, float, str, int, int],
    ) -> int: ...


class _RateLimitClient(Protocol):
    def register_script(self, script: str) -> _RateLimitScript: ...

    def ping(self) -> bool: ...

    def close(self) -> None: ...


class _RedisRateLimiter:
    """One shared sorted set per route and client. Redis runs the update atomically."""

    def __init__(self, max_requests: int, scope: str, client: _RateLimitClient) -> None:
        if max_requests <= 0:
            raise ValueError(f"max_requests must be positive, got {max_requests}")
        self.max_requests = max_requests
        self._scope = scope
        self._script = client.register_script(_RATE_LIMIT_SCRIPT)

    def allow(self, key: str, now: float) -> bool:
        allowed = self._script(
            keys=(f"ratelimit:{self._scope}:{key}",),
            args=(
                now,
                _RATE_LIMIT_WINDOW,
                f"{now}:{time.time_ns()}",
                self.max_requests,
                int(_RATE_LIMIT_WINDOW),
            ),
        )
        return int(allowed) == 1


_client: _RateLimitClient | None = None
_client_lock = threading.Lock()


def configure_rate_limit(url: str, socket_timeout: float = _REDIS_SOCKET_TIMEOUT) -> _RateLimitClient:
    """Open the process-wide client. Every worker must point at the same Redis."""
    from redis import Redis

    if not url:
        raise ValueError("redis url must not be empty")
    if socket_timeout <= 0:
        raise ValueError(f"socket_timeout must be positive, got {socket_timeout}")
    client = cast(_RateLimitClient, Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        url,
        socket_connect_timeout=socket_timeout,
        socket_timeout=socket_timeout,
        decode_responses=True,
    ))
    client.ping()
    _replace_client(client)
    return client


def close_rate_limit() -> None:
    client = _replace_client(None)
    if client is None:
        return
    try:
        client.close()
    except Exception:
        log.error("rate limit store close failed", exc_info=True)


def _replace_client(client: _RateLimitClient | None) -> _RateLimitClient | None:
    global _client
    with _client_lock:
        previous = _client
        _client = client
        return previous


def _get_client() -> _RateLimitClient:
    with _client_lock:
        client = _client
    if client is None:
        raise RuntimeError("redis rate limiter is not configured")
    return client


def _redis_config(cfg: object) -> tuple[str, float]:
    getter = getattr(cfg, "get", None)
    if getter is None:
        raise RuntimeError("redis config is required")
    raw = cast(object, getter("redis", None))
    if not isinstance(raw, Mapping):
        raise RuntimeError("redis config is required")
    section = cast(Mapping[str, object], raw)
    url = section.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("redis.url must be a non-empty string")
    timeout = section.get("socket_timeout", _REDIS_SOCKET_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError(f"redis.socket_timeout must be positive, got {timeout}")
    return url, float(timeout)


def configure_rate_limit_from_config(cfg: object) -> _RateLimitClient:
    return configure_rate_limit(*_redis_config(cfg))


def rate_limit[**P, R](max_requests: int) -> Callable[
    [Decorated[BaseApi, P, R]],
    DecoratedReturn[BaseApi, P, R],
]:
    """Rate-limit a handler by client IP using a shared Redis sliding window."""
    if max_requests <= 0:
        raise ValueError(f"max_requests must be positive, got {max_requests}")

    def decorator(
        f: Decorated[BaseApi, P, R]
    ) -> DecoratedReturn[BaseApi, P, R]:
        scope = f"{f.__module__}.{f.__qualname__}:{max_requests}"
        shared: _RedisRateLimiter | None = None
        seen: _RateLimitClient | None = None

        @wraps(f)
        def wrapper(self: BaseApi, *args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]:
            nonlocal shared, seen
            ip = request.remote_addr or "<unknown>"
            try:
                client = _get_client()
                if shared is None or seen is not client:
                    shared = _RedisRateLimiter(max_requests, scope, client)
                    seen = client
                allowed = shared.allow(ip, time.time())
            except Exception:
                log.error("rate limit store failed", exc_info=True)
                return err(msg="Too many requests.", code=429)
            if not allowed:
                return err(msg="Too many requests.", code=429)
            return f(self, *args, **kwargs)
        return cast(DecoratedReturn[BaseApi, P, R], wrapper)
    return decorator
