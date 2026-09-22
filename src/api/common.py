from pathlib import Path
from custom_types import HTTPMethod
from typing import NamedTuple
from abc import ABC
from config import ConfigLike
from core import Subscription
from bwatch import BWatch
from flask import Flask, Response
from loggers import Logger

type ResponseType = tuple[Response, int] | Response

RES_DIR = Path(__file__).resolve().parent.parent.parent / 'res'


class Route(NamedTuple):
    """A Flask route, used in BaseApi."""
    method: HTTPMethod
    path: str
    handler: str
    rate_limit: int | None = None

    def validate(self) -> None:
        if not self.path.startswith('/'):
            raise ValueError(f"Route path must start with '/', got '{self.path}'")
        if self.rate_limit is not None and self.rate_limit <= 0:
            raise ValueError(f"rate_limit must be positive, got {self.rate_limit}")

    def register(self, api: BaseApi) -> None:
        from .decorators.rate_limit import rate_limit # intentional lazy loading
        try:
            func = getattr(type(api), self.handler)
        except AttributeError:
            api.log.critical(f"method {self.handler} doesnt exist!")
            return
        if self.rate_limit is not None:
            func = rate_limit(self.rate_limit)(func)
        func = func.__get__(api, type(api))
        
        url = '/' + '/'.join([
            p.strip('/') for p in (api.uri, self.path) if p.strip('/')
        ])
        
        api.app.add_url_rule(url, self.handler, func, methods=[self.method])

class BaseApi(ABC):
    """Base class for API handlers. Enforces required attributes and route registration."""
    
    ROUTES: list[Route]  # subclasses must define this
    
    def __init__(self,
                 app: Flask, 
                 cfg: ConfigLike, 
                 sub: Subscription, 
                 bw: BWatch,
                 uri: str):
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.app = app
            self.cfg = cfg
            self.sub = sub
            self.bw = bw
            self.uri = uri
            self._register_routes()
            self.reg_handles()

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Called when a class inherits from BaseApi. Validates at import time."""
        super().__init_subclass__(**kwargs)
        
        if not hasattr(cls, 'ROUTES'):
            raise TypeError(f"{cls.__name__} must define ROUTES")
        
        for route in cls.ROUTES:
            route.validate()
            if not hasattr(cls, route.handler):
                raise TypeError(
                    f"{cls.__name__}.ROUTES references '{route.handler}' "
                    f"but no such method exists"
                )
    
    def _register_routes(self) -> None:
        for route in self.ROUTES:
            route.register(self)
    
    def reg_handles(self) -> None:
        """Optional: subclass setup beyond route registration (error handlers, etc)."""
        pass
