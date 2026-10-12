from __future__ import annotations

import os
from typing import TYPE_CHECKING

from api.application import create_api_application
from app import AppOptions, create_application

if TYPE_CHECKING:
    from app import Application

options = AppOptions(
    require_proxy=os.getenv("REQUIRE_PROXY", "1").lower() not in {"0", "false", "no"},
)
app = create_api_application(proxy_hops=options.proxy_hops, require_proxy=options.require_proxy)
runtime: Application | None = None


@app.before_serving
async def start() -> None:
    global runtime  # ruff: ignore[global-statement]
    runtime = await create_application(options=options, app=app)


@app.after_serving
async def stop() -> None:
    if runtime is not None:
        await runtime.stop()
