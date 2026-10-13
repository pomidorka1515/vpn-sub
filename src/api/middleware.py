from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl

from loggers import Logger, color_status
from loggers.level import TRACE, env_level

if TYPE_CHECKING:
    from hypercorn.typing import (
        ASGIFramework,
        ASGIReceiveCallable,
        ASGISendCallable,
        ASGISendEvent,
        Scope,
    )

log = Logger("HTTP", level=env_level("LOGLEVEL_HTTP", logging.INFO))
log.parent = logging.getLogger()


class ProxyMiddleware:
    """Inspect the socket peer before trusting forwarded values; log no secrets."""

    def __init__(self, app: ASGIFramework, *, hops: int = 1, require_proxy: bool = False) -> None:
        if hops < 0:
            raise ValueError("proxy hops must be non-negative")
        self.app = app
        self.hops = hops
        self.require_proxy = require_proxy
        log.setLevel(
            env_level(
                "LOGLEVEL_HTTP",
                logging.INFO,
            )
        )

    async def __call__(
        self,
        scope: Scope,
        receive: ASGIReceiveCallable,
        send: ASGISendCallable,
    ) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        peer = scope.get("client")  # pyright: ignore[reportUnknownMemberType]  # Upstream extensions contain unparameterized dicts.
        trusted = peer is not None and peer[0] in {"127.0.0.1", "::1"}
        if self.require_proxy and not trusted:
            if scope["type"] == "websocket":
                await send(
                    {"type": "websocket.close", "code": 1008, "reason": "direct access forbidden"},
                )
                return
            body = b'{"success": false, "msg": "direct access forbidden", "obj": null}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [(b"content-type", b"application/json")],
                },
            )
            await send({"type": "http.response.body", "body": body, "more_body": False})
            return
        scope = scope.copy()
        if trusted and self.hops:
            headers = dict(scope["headers"])
            addresses = headers.get(b"x-forwarded-for", b"").decode("latin1").split(",")
            if len(addresses) >= self.hops and addresses[-self.hops].strip():
                scope["client"] = (addresses[-self.hops].strip(), 0)
            scheme = headers.get(b"x-forwarded-proto", b"").decode("latin1").split(",")[-1].strip()
            if scheme in {"http", "https"}:
                scope["scheme"] = (
                    scheme if scope["type"] == "http" else {"http": "ws", "https": "wss"}[scheme]
                )
            host = headers.get(b"x-forwarded-host", b"").split(b",")[-1].strip()
            if host:
                scope["headers"] = [
                    (key, value) for key, value in scope["headers"] if key != b"host"
                ] + [(b"host", host)]
        status = 500
        sent = 0

        async def logged_send(message: ASGISendEvent) -> None:
            nonlocal status, sent
            # ASGI messages have a tagged union type; narrow it before access.
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                sent += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, logged_send)
        finally:
            if scope["type"] == "http":
                path = scope["path"]
                level = (
                    TRACE
                    if 200 <= status < 300 and path.rstrip("/").endswith("/api/state/polling")
                    else logging.DEBUG
                    if 200 <= status < 300
                    else logging.INFO
                )
                client = scope.get("client")  # pyright: ignore[reportUnknownMemberType]  # Upstream extensions contain unparameterized dicts.
                target = path
                query = scope.get("query_string", b"").decode("latin1")
                if any(key == "token" for key, _ in parse_qsl(query, keep_blank_values=True)):
                    target += "?token=..."
                user_agent = dict(scope["headers"]).get(b"user-agent", b"-").decode("latin1")
                user_agent = (
                    re.sub(
                        r"(?i)\b(token|password|secret|authorization)=([^\s&;]+)",
                        r"\1=...",
                        user_agent,
                    )
                    .replace('"', '\\"')
                    .replace("\r", "")
                    .replace("\n", "")
                )
                log.log(
                    level,
                    '%s > "%s %s HTTP/%s" %s %db "%s"',
                    (client or ("-", 0))[0],
                    scope["method"],
                    target,
                    scope["http_version"],
                    color_status(str(status)),
                    sent,
                    user_agent,
                )
