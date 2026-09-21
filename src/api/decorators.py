import binascii

from typing import Callable, Concatenate, cast, TYPE_CHECKING
from flask import request, g, Response

if TYPE_CHECKING:
    from .webapi import WebApi

type Decorated[
    API_T: BaseApi, 
    **P = ..., 
    R = object
] = Callable[Concatenate[API_T, P], R]

type DecoratedInject[
    API_T: BaseApi, 
    I: object,
    **P = ..., 
    R = object
] = Callable[Concatenate[API_T, I, P], R]

type DecoratedReturn[
    API_T: BaseApi,
    **P = ...,
    R = object
] = Callable[Concatenate[API_T, P], WrappedReturn[R]]

type WrappedReturn[
    R
] = R | tuple[Response, int]

def _parse_basic_auth(header: str) -> tuple[str, str] | None:
    """Parse 'Basic <base64>' header. 
    
    Returns:
        (username, password) or None
    """
    if not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:]).decode()
        if ":" not in decoded:
            return None
        user, pw = decoded.split(":", 1)
        return user, pw
    except (ValueError, UnicodeError, binascii.Error):
        return None

# ── Auth decorators ──────────────────────────────────────────────
def requires_admin_auth[**P, R](f: Decorated[Api, P, R]) -> Decorated[Api, P, R]:
    """
    Admin API auth via Authorization header. Returns 401 on failure."""
    @wraps(f)
    def wrapper(self: Api, *args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]: 
        provided = request.headers.get('Authorization', '')
        if not provided or not compare(provided, self.token):
            return err("Unauthorized", 401)
        return f(self, *args, **kwargs)
    return cast(Decorated[BaseApi, P, R], wrapper)
def requires_basic_admin_auth[**P, R](f: Decorated[Api, P, R]) -> Decorated[Api, P, R]:
    """Admin API auth via Basic auth header. Returns 401 on failure."""
    @wraps(f)
    def wrapper(self: Api, *args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]:
        provided = request.headers.get("Authorization", "")
        creds = _parse_basic_auth(provided)
        valid: tuple[str, str] = tuple(self.cfg["api_admin_ui_auth"])
        err = Response("Unauthorized")
        err.headers["WWW-Authenticate"] = 'Basic realm="Admin UI"'
        if not creds:
            return err, 401
        user, pw = creds
        if (not compare(user, valid[0])) or (not compare(pw, valid[1])):
            return err, 401
        return f(self, *args, **kwargs)
    return cast(Decorated[BaseApi, P, R], wrapper)
def requires_webapi_auth[**P, R](f: DecoratedInject[WebApi, str, P, R]) -> Decorated[WebApi, P, R]:
    """WebApi auth via token cookie. Injects `username` as first arg after self.
    Returns 401 on failure."""
    @wraps(f)
    def wrapper(self: WebApi, *args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]:
        auth_token = request.cookies.get('auth_token')
        username = self.validate_auth_token(auth_token)
        if not username:
            return err("Invalid auth token.", 401)
        return f(self, username, *args, **kwargs)
    return cast(Decorated[BaseApi, P, R], wrapper)
def requires_no_auth[**P, R](f: Decorated[WebApi, P, R]) -> Decorated[WebApi, P, R]:
    """WebApi: reject if already authenticated (for register). Returns 403."""
    @wraps(f)
    def wrapper(self: WebApi, *args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]:
        auth_token = request.cookies.get('auth_token')
        if auth_token and self.validate_auth_token(auth_token):
            return err("Must not be authorized.", 403)
        return f(self, *args, **kwargs)
    return cast(Decorated[BaseApi, P, R], wrapper)
# ── Rate limiting ────────────────────────────────────────────────
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
# ── Validation decorators ────────────────────────────────────────
def requires_fields[**P, R](*fields: str) -> Callable[[Callable[P, R]], Callable[P, WrappedReturn[R]]]:
    """Validate request JSON object, store it on flask.g.json_obj, and require named fields."""
    def decorator(f: Callable[P, R]) -> Callable[P, WrappedReturn[R]]:
        @wraps(f)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]:
            content = request.get_json(silent=True)
            if content is None:
                return err("Missing JSON data.", 400)
            if not isinstance(content, dict):
                return err("Body must be a JSON dict.", 400)

            missing = [x for x in fields if x not in content]
            if missing:
                return err(f"Missing fields: {', '.join(missing)}", 400)

            content = cast(dict[str, JsonifyValue], content) # NOTE: g.json_obj will appear
                                                             # NOTE: as dict[Unknown, Unknown] -> type errors without this
            g.json_obj = content
            return f(*args, **kwargs)
        return cast(Callable[P, WrappedReturn[R]], wrapper)
    return decorator
def requires_fields_strict[**P, R](*fields: tuple[str, type[JsonifyValue]]) -> Callable[
    [Callable[P, R]],
    Callable[P, WrappedReturn[R]]
]:
    """Validate request JSON object, store it on flask.g.json_obj, and require named fields with strict type checking."""
    def decorator(f: Callable[P, R]) -> Callable[P, WrappedReturn[R]]:
        @wraps(f)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]:
            content = request.get_json(silent=True)
            if content is None:
                return err("Missing JSON data.", 400)
            if not isinstance(content, dict):
                return err("Body must be a JSON dict.", 400)
            content = cast(dict[str, object], content)

            for field, expected_type in fields:
                if field not in content:
                    return err(f"Missing field: {field}", 400)
                value = content[field]
                # None is allowed for optional fields
                if value is not None and not isinstance(value, expected_type):
                    return err(
                        f"Field '{field}' must be {expected_type.__name__}, got {type(value).__name__}",
                        400
                    )
            content = cast(dict[str, JsonifyValue], content)
            g.json_obj = content
            return f(*args, **kwargs)
        return cast(Callable[P, WrappedReturn[R]], wrapper)
    return decorator
def requires_args[**P, R](*arg: str) -> Callable[[Callable[P, R]], Callable[P, WrappedReturn[R]]]:
    """Validate request.args has all named fields. Returns 400 on failure."""
    def decorator(f: Callable[P, R]) -> Callable[P, WrappedReturn[R]]:
        @wraps(f)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]:
            content = request.args
            missing = [x for x in arg if x not in content]
            if missing:
                return err(f"Missing args: {', '.join(missing)}", 400)
            return f(*args, **kwargs)
        return cast(Callable[P, WrappedReturn[R]], wrapper)
    return decorator


