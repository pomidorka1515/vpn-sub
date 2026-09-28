from pathlib import Path
from custom_types import HTTPMethod
from typing import NamedTuple, cast
from abc import ABC
from collections.abc import Mapping
from functools import lru_cache
from hashlib import sha1
from config import ConfigLike
from core import Subscription
from bwatch import BWatch
from flask import Flask, Response, request
from loggers import Logger

type ResponseType = tuple[Response, int] | Response

RES_DIR = Path(__file__).resolve().parent.parent.parent / 'res'


@lru_cache(maxsize=1)
def asset_version() -> str:
    """Cache-busting version for static assets, derived from common.js content."""
    data = (RES_DIR / 'common.js').read_bytes()
    return sha1(data).hexdigest()[:12]


WEB_LANGS: tuple[str, ...] = ('en', 'ru')
_WEB_PAGES: tuple[str, ...] = ('auth', 'dashboard', 'history', 'admin')


def resolve_web_lang() -> str:
    """?lang= wins, then the lang cookie, then English."""
    for raw in (request.args.get('lang'), request.cookies.get('lang')):
        code = (raw or '').lower()
        if code in WEB_LANGS:
            return code
    return 'en'


def _string_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: text
        for key, text in cast(Mapping[object, object], value).items()
        if isinstance(key, str) and isinstance(text, str)
    }


def _section(web: Mapping[str, object], name: str) -> Mapping[str, object]:
    section = web.get(name)
    if isinstance(section, Mapping):
        return cast(Mapping[str, object], section)
    return {}


def web_lang_tables(lang_cfg: ConfigLike, page: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """Active page strings, plus the English table used when a key is missing."""
    if page not in _WEB_PAGES:
        raise ValueError(f"unknown web page '{page}'")
    lang = resolve_web_lang()
    raw = lang_cfg.get('web')
    web: Mapping[str, object] = (
        cast(Mapping[str, object], raw) if isinstance(raw, Mapping) else {}
    )
    shared = _section(web, 'shared')
    page_table = _section(web, page)

    def merged(code: str) -> dict[str, str]:
        out = _string_map(shared.get(code))
        out.update(_string_map(page_table.get(code)))
        return out

    return lang, merged(lang), merged('en')


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
