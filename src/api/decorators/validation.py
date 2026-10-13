from __future__ import annotations

from functools import wraps
from typing import TYPE_CHECKING, cast

from quart import g, request

from util import err

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from custom_types import JsonifyValue

    from ._common import WrappedReturn


def requires_fields[**P, R](
    *fields: str,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[WrappedReturn[R]]]]:
    """Validate request JSON object, store it on quart.g.json_obj, and require named fields."""

    def decorator(f: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[WrappedReturn[R]]]:
        @wraps(f)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]:
            content = await request.get_json(silent=True)
            if content is None:
                return err("Missing JSON data.", 400)
            if not isinstance(content, dict):
                return err("Body must be a JSON dict.", 400)

            missing = [x for x in fields if x not in content]
            if missing:
                return err(f"Missing fields: {', '.join(missing)}", 400)

            content = cast("dict[str, JsonifyValue]", content)  # NOTE: g.json_obj will appear
            # NOTE: as dict[Unknown, Unknown] -> type errors without this
            g.json_obj = content
            return await f(*args, **kwargs)

        return wrapper

    return decorator


def requires_fields_strict[**P, R](
    *fields: tuple[str, type[JsonifyValue]],
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[WrappedReturn[R]]]]:
    """Validate request JSON object, store it on quart.g.json_obj, and require named fields with strict type checking."""

    def decorator(f: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[WrappedReturn[R]]]:
        @wraps(f)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]:
            content = await request.get_json(silent=True)
            if content is None:
                return err("Missing JSON data.", 400)
            if not isinstance(content, dict):
                return err("Body must be a JSON dict.", 400)
            content = cast("dict[str, object]", content)

            for field, expected_type in fields:
                if field not in content:
                    return err(f"Missing field: {field}", 400)
                value = content[field]
                # None is allowed for optional fields
                if value is not None and not isinstance(value, expected_type):
                    return err(
                        f"Field '{field}' must be {expected_type.__name__}, got {type(value).__name__}",
                        400,
                    )
            content = cast("dict[str, JsonifyValue]", content)
            g.json_obj = content
            return await f(*args, **kwargs)

        return wrapper

    return decorator


def requires_args[**P, R](
    *arg: str,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[WrappedReturn[R]]]]:
    """Validate request.args has all named fields. Returns 400 on failure."""

    def decorator(f: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[WrappedReturn[R]]]:
        @wraps(f)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> WrappedReturn[R]:
            content = request.args
            missing = [x for x in arg if x not in content]
            if missing:
                return err(f"Missing args: {', '.join(missing)}", 400)
            return await f(*args, **kwargs)

        return wrapper

    return decorator
