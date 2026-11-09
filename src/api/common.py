from __future__ import annotations

from abc import ABC
from collections.abc import Mapping
from functools import lru_cache
from hashlib import sha1
from typing import TYPE_CHECKING, ClassVar, NamedTuple, Protocol, cast

from quart import Response, request
from werkzeug.wrappers import Response as WerkzeugResponse

from loggers import Logger
from paths import frontend_root

if TYPE_CHECKING:
    from quart import Quart

    from app_runtime.rate_limit import AsyncRateLimiter
    from config import AppConfig, LangConfig
    from config.store import AsyncConfig
    from core import Subscription
    from core.common import Snapshot
    from custom_types import HTTPMethod, JsonifyValue

type ResponseType = tuple[Response, int] | Response | WerkzeugResponse

RES_DIR = frontend_root()


class SnapshotStatus(Protocol):
    def get_daily_snapshot_failure(self) -> JsonifyValue: ...


@lru_cache(maxsize=1)
def asset_version() -> str:
    """Cache-busting version for static assets, derived from shared frontend files."""
    digest = sha1(usedforsecurity=False)
    for path in sorted(RES_DIR.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(RES_DIR).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


WEB_LANGS: tuple[str, ...] = ("en", "ru")
_WEB_PAGES: tuple[str, ...] = ("auth", "dashboard", "history", "admin", "admin_auth", "error")


def resolve_web_lang() -> str:
    """?lang= wins, then the lang cookie, then English."""
    for raw in (request.args.get("lang"), request.cookies.get("lang")):
        code = (raw or "").lower()
        if code in WEB_LANGS:
            return code
    return "en"


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


def web_lang_tables(
    lang_cfg: Snapshot[LangConfig],
    page: str,
    language_code: str | None = None,
) -> tuple[str, dict[str, str], dict[str, str]]:
    """Active page strings, plus the English table used when a key is missing."""
    if page not in _WEB_PAGES:
        raise ValueError(f"unknown web page '{page}'")
    lang = language_code or resolve_web_lang()
    language = lang_cfg.view()
    raw = cast(object, language.get("web"))
    web: Mapping[str, object] = cast(Mapping[str, object], raw) if isinstance(raw, Mapping) else {}
    shared = _section(web, "shared")
    page_table = _section(web, page)

    def merged(code: str) -> dict[str, str]:
        out = _string_map(shared.get(code))
        out.update(_string_map(page_table.get(code)))
        return out

    return lang, merged(lang), merged("en")


class Route(NamedTuple):
    """A Quart route, used in AsyncBaseApi."""

    method: HTTPMethod
    path: str
    handler: str
    rate_limit: int | None = None

    def validate(self) -> None:
        if not self.path.startswith("/"):
            raise ValueError(f"Route path must start with '/', got '{self.path}'")
        if self.rate_limit is not None and self.rate_limit <= 0:
            raise ValueError(f"rate_limit must be positive, got {self.rate_limit}")

    def register(self, api: AsyncBaseApi) -> None:
        from .decorators.rate_limit import rate_limit  # intentional lazy loading

        try:
            func = getattr(type(api), self.handler)
        except AttributeError:
            api.log.critical(f"method {self.handler} doesnt exist!")
            return
        if self.rate_limit is not None:
            func = rate_limit(self.rate_limit)(func)
        func = func.__get__(api, type(api))

        url = "/" + "/".join([p.strip("/") for p in (api.uri, self.path) if p.strip("/")])

        api.app.add_url_rule(url, self.handler, func, methods=[self.method])


class AsyncBaseApi(ABC):
    """Base class for API handlers. Enforces required attributes and route registration."""

    ROUTES: ClassVar[tuple[Route, ...]]

    def __init__(
        self,
        app: Quart,
        cfg: AsyncConfig[AppConfig],
        sub: Subscription,
        bw: SnapshotStatus,
        uri: str,
        limiter: AsyncRateLimiter,
    ) -> None:
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.app = app
            self.cfg = cfg
            self.sub = sub
            self.bw = bw
            self.uri = uri
            self.limiter = limiter
            self._register_routes()
            self.reg_handles()

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Called when a class inherits from AsyncBaseApi. Validates at import time."""
        super().__init_subclass__(**kwargs)

        if not hasattr(cls, "ROUTES"):
            raise TypeError(f"{cls.__name__} must define ROUTES")

        for route in cls.ROUTES:
            route.validate()
            if not hasattr(cls, route.handler):
                raise TypeError(
                    f"{cls.__name__}.ROUTES references '{route.handler}' but no such method exists",
                )

    def _register_routes(self) -> None:
        for route in self.ROUTES:
            route.register(self)

    def reg_handles(self) -> None:  # ruff: ignore[empty-method-without-abstract-decorator]
        """Optional: subclass setup beyond route registration (error handlers, etc)."""
