from functools import wraps
from typing import Callable, cast
from util import err
from custom_types import JsonifyValue
from ._common import WrappedReturn
from flask import request, g

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
