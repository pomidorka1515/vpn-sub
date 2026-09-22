from typing import Callable, Concatenate, TYPE_CHECKING
from flask import Response
if TYPE_CHECKING:
    from ..common import BaseApi

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
