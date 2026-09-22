from ._common import Decorated, DecoratedReturn, WrappedReturn
from collections import OrderedDict, deque
from util import err
from flask import request
from functools import wraps
from typing import cast, Callable
import threading
import time

from ..common import BaseApi

_RATE_LIMIT_WINDOW = 60.0

class _RateLimiter:
    """Thread-safe sliding-window limiter for one route instance."""

    def __init__(self, max_requests: int, max_buckets: int = 10_000) -> None:
        if max_requests <= 0:
            raise ValueError(f"max_requests must be positive, got {max_requests}")
        if max_buckets <= 0:
            raise ValueError(f"max_buckets must be positive, got {max_buckets}")
        self.max_requests = max_requests
        self.max_buckets = max_buckets
        self._buckets: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str, now: float) -> bool:
        with self._lock:
            self._remove_inactive(now)
            timestamps = self._buckets.get(key)
            if timestamps is None:
                if len(self._buckets) >= self.max_buckets:
                    self._buckets.popitem(last=False)
                timestamps = deque[float]()
                self._buckets[key] = timestamps

            while timestamps and now - timestamps[0] >= _RATE_LIMIT_WINDOW:
                timestamps.popleft()

            if len(timestamps) >= self.max_requests:
                return False

            timestamps.append(now)
            self._buckets.move_to_end(key)
            return True

    @property
    def bucket_count(self) -> int:
        with self._lock:
            return len(self._buckets)

    def bucket_keys(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._buckets)

    def _remove_inactive(self, now: float) -> None:
        """Remove expired buckets from the oldest end of the access order."""
        while self._buckets:
            key, timestamps = next(iter(self._buckets.items()))
            last_request = timestamps[-1] if timestamps else now
            if now - last_request < _RATE_LIMIT_WINDOW:
                return
            del self._buckets[key]


def rate_limit[**P, R](max_requests: int, max_buckets: int = 10_000) -> Callable[
    [Decorated[BaseApi, P, R]],
    DecoratedReturn[BaseApi, P, R],
]:
    """Rate-limit a handler by client IP using a sliding time window."""
    if max_requests <= 0:
        raise ValueError(f"max_requests must be positive, got {max_requests}")
    if max_buckets <= 0:
        raise ValueError(f"max_buckets must be positive, got {max_buckets}")

    def decorator(
        f: Decorated[BaseApi, P, R]
    ) -> DecoratedReturn[BaseApi, P, R]:
        limiter = _RateLimiter(max_requests, max_buckets)

        @wraps(f)
        def wrapper(self: BaseApi, *args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]:
            ip = request.remote_addr or "<unknown>"
            if not limiter.allow(ip, time.monotonic()):
                return err(msg="Too many requests.", code=429)
            return f(self, *args, **kwargs)
        return cast(DecoratedReturn[BaseApi, P, R], wrapper)
    return decorator
