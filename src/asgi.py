from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from api.application import create_api_application
from app import AppOptions, create_application
from serve import RUNTIME_SHUTDOWN_TIMEOUT
from util import err

if TYPE_CHECKING:
    from quart import Quart, Response

    from app import Application

type RuntimeFactory = Callable[..., Awaitable[Application]]


def create_asgi_application(
    options: AppOptions | None = None,
    *,
    factory: RuntimeFactory = create_application,
) -> Quart:
    options = options or AppOptions(
        require_proxy=os.getenv("REQUIRE_PROXY", "1").lower() not in {"0", "false", "no"},
    )
    application = create_api_application(
        proxy_hops=options.proxy_hops,
        require_proxy=options.require_proxy,
    )
    application.config["BACKGROUND_TASK_SHUTDOWN_TIMEOUT"] = RUNTIME_SHUTDOWN_TIMEOUT

    @application.before_serving
    async def start() -> None:
        runtime = await factory(options=options, app=application)
        application.extensions["runtime"] = runtime

    @application.before_request
    async def require_ready() -> tuple[Response, int] | None:
        runtime = application.extensions.get("runtime")
        if runtime is None or not runtime.ready:
            return err("Service unavailable", 503)
        return None

    @application.after_serving
    async def stop() -> None:
        runtime = application.extensions.get("runtime")
        if runtime is not None:
            # Runtime.stop retains cleanup ownership on timeout. Never report
            # successful shutdown or close connections beneath unsettled work.
            async with asyncio.timeout(RUNTIME_SHUTDOWN_TIMEOUT):
                await runtime.stop()

    return application


app = create_asgi_application()
