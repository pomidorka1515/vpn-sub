from __future__ import annotations

from typing import TYPE_CHECKING

from quart import request

from builders import get_subscription
from loggers import Logger
from util import err

if TYPE_CHECKING:
    from quart import Quart, Response

    from app_runtime.rate_limit import AsyncRateLimiter
    from core import Subscription


log = Logger("rate_limit")


def register_subscription_route(app: Quart, sub: Subscription, limiter: AsyncRateLimiter) -> None:
    uri = sub.res.cfg.view()["uri"].strip("/")

    @app.route(f"/{uri}", strict_slashes=False)
    async def subscription() -> tuple[Response, int]:  # pyright: ignore[reportUnusedFunction]
        try:
            allowed = await limiter.allow("subscription", request.remote_addr or "<unknown>", 60)
        except Exception:
            log.error("rate limit store failed")  # ruff: ignore[error-instead-of-exception] credentials may occur in exception text
            return err("Too many requests.", 429)
        if not allowed:
            return err("Too many requests.", 429)
        return await get_subscription(
            sub,
            token=request.args.get("token", ""),
            lang=request.args.get("lang", ""),
            ua=request.headers.get("User-Agent", ""),
            ip=request.remote_addr or "",
            force_json=request.args.get("force_json", "0"),
        )
