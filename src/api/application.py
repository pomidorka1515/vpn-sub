from __future__ import annotations

from typing import TYPE_CHECKING

from jinja2 import FileSystemLoader
from quart import Quart, request
from quart.json.provider import DefaultJSONProvider
from werkzeug.exceptions import HTTPException

from errors import AppError
from loggers import Logger
from util import err

from .common import RES_DIR
from .middleware import ProxyMiddleware

if TYPE_CHECKING:
    from quart import Response

log = Logger("api")


def create_api_application(*, proxy_hops: int = 1, require_proxy: bool = False) -> Quart:
    app = Quart(__name__)
    app.json = DefaultJSONProvider(app)
    app.json.sort_keys = False
    app.jinja_env.loader = FileSystemLoader(str(RES_DIR))
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    app.asgi_app = ProxyMiddleware(app.asgi_app, hops=proxy_hops, require_proxy=require_proxy)  # type: ignore[method-assign]

    @app.errorhandler(AppError)
    async def handle_app_error(error: AppError) -> tuple[Response, int]:
        return err(error.message, error.status)

    @app.errorhandler(HTTPException)
    async def handle_http_error(error: HTTPException) -> tuple[Response, int]:
        return err(error.description, error.code or 500)

    @app.errorhandler(Exception)
    async def handle_unexpected_error(error: Exception) -> tuple[Response, int]:
        # Query strings, headers and exception text can contain credentials.
        log.error("unhandled %s on %s %s", type(error).__name__, request.method, request.path)
        return err("Internal server error", 500)

    return app
