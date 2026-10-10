from typing import Concatenate
from collections.abc import Callable
from flask import Response

type Decorated[
    API_T,
    **P = ...,
    R = object
] = Callable[Concatenate[API_T, P], R]

type DecoratedInject[
    API_T,
    I: object,
    **P = ...,
    R = object
] = Callable[Concatenate[API_T, I, P], R]

type DecoratedReturn[
    API_T,
    **P = ...,
    R = object
] = Callable[Concatenate[API_T, P], WrappedReturn[R]]

type WrappedReturn[
    R
] = R | tuple[Response, int]
