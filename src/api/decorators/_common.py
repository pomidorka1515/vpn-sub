from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Concatenate

from quart import Response

type Decorated[API_T, **P = ..., R = object] = Callable[Concatenate[API_T, P], Awaitable[R]]

type DecoratedInject[API_T, I: object, **P = ..., R = object] = Callable[
    Concatenate[API_T, I, P],
    Awaitable[R],
]

type DecoratedReturn[API_T, **P = ..., R = object] = Callable[
    Concatenate[API_T, P],
    Awaitable[WrappedReturn[R]],
]

type WrappedReturn[R] = R | tuple[Response, int]
