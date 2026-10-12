from __future__ import annotations

from typing import TYPE_CHECKING

from quart import request

from builders import get_subscription

if TYPE_CHECKING:
    from quart import Quart, Response

    from core import AsyncSubscription


def register_subscription_route(app: Quart, sub: AsyncSubscription) -> None:
    uri = sub.res.cfg.view()["uri"].strip("/")

    @app.route(f"/{uri}", strict_slashes=False)
    async def subscription() -> tuple[Response, int]:  # pyright: ignore[reportUnusedFunction]
        return await get_subscription(
            sub,
            token=request.args.get("token", ""),
            lang=request.args.get("lang", ""),
            ua=request.headers.get("User-Agent", ""),
            ip=request.remote_addr or "",
            force_json=request.args.get("force_json", "0"),
        )
