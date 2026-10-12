from __future__ import annotations

from typing import TYPE_CHECKING

from flask import request

from builders import get_subscription

if TYPE_CHECKING:
    from flask import Flask, Response

    from core import Subscription


def register_subscription_route(app: Flask, sub: Subscription) -> None:
    uri = sub.res.cfg.view()["uri"].strip("/")

    @app.route(f"/{uri}", strict_slashes=False)
    def subscription() -> tuple[Response, int]:  # pyright: ignore[reportUnusedFunction]
        return get_subscription(sub, token=request.args.get("token", ""), lang=request.args.get("lang", ""), ua=request.headers.get("User-Agent", ""), ip=request.headers.get("X-Real-IP", ""), force_json=request.args.get("force_json", "0"))
