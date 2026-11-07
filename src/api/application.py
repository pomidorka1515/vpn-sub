from __future__ import annotations

from jinja2 import FileSystemLoader
from quart import Quart, Response, render_template, request
from quart.json.provider import DefaultJSONProvider
from werkzeug.exceptions import HTTPException

from app_runtime.tasks import CapacityExceededError
from errors import AppError, CommittedAuditError
from loggers import Logger
from threadutil import named_to_thread
from util import err, isbrowser, ok

from .common import RES_DIR, asset_version
from .middleware import ProxyMiddleware
from .web.static import AsyncStaticRoutes

log = Logger("api")


def create_api_application(*, proxy_hops: int = 1, require_proxy: bool = False) -> Quart:
    page_endpoints = {route.handler for route in AsyncStaticRoutes.ROUTES}
    app = Quart(__name__)
    app.json = DefaultJSONProvider(app)
    app.json.sort_keys = False
    app.jinja_env.loader = FileSystemLoader(str(RES_DIR / "templates"))
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    app.asgi_app = ProxyMiddleware(app.asgi_app, hops=proxy_hops, require_proxy=require_proxy)  # type: ignore[method-assign]

    @app.after_request
    async def browser_not_found(response: Response) -> Response:
        if (
            response.status_code == 404
            and (request.url_rule is None or request.endpoint in page_endpoints)
            and isbrowser(request.headers.get("User-Agent", ""))
        ):
            web_api: AsyncStaticRoutes | None = app.extensions.get("frontend_web_api")
            if web_api is None:
                prefix = ""
                version = await named_to_thread("frontend:asset-version", asset_version)
            else:
                prefix = web_api.prefix
                version = await web_api.sub.res.offloads.files.run(asset_version)
            html = await render_template("404.html", prefix=prefix, asset_version=version)
            return Response(html, status=404, mimetype="text/html")
        return response

    @app.errorhandler(AppError)
    async def handle_app_error(error: AppError) -> tuple[Response, int]:
        if isinstance(error, CommittedAuditError):
            return ok(
                error.message,
                error.status,
                obj={
                    **(
                        error.result if isinstance(error.result, dict) else {"result": error.result}
                    ),
                    "committed": True,
                    "audit_pending": True,
                },
            )
        return err(error.message, error.status)

    @app.errorhandler(CapacityExceededError)
    async def handle_capacity_error(_error: CapacityExceededError) -> tuple[Response, int]:
        response, status = err("Service temporarily busy. Retry after 1 second.", 503)
        response.headers["Retry-After"] = "1"
        return response, status

    @app.errorhandler(HTTPException)
    async def handle_http_error(error: HTTPException) -> tuple[Response, int]:
        response, status = err(error.description, error.code or 500)
        response.headers.extend(
            (name, value)
            for name, value in error.get_response().headers
            if name.lower() not in {"content-type", "content-length"}
        )
        return response, status

    @app.errorhandler(Exception)
    async def handle_unexpected_error(error: Exception) -> tuple[Response, int]:
        # Query strings, headers and exception text can contain credentials.
        log.error("unhandled %s on %s %s", type(error).__name__, request.method, request.path)
        return err("Internal server error", 500)

    return app
