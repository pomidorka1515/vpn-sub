"""In-process dispatch through the production API's validation and auth boundary."""
from __future__ import annotations

from typing import TYPE_CHECKING, cast
from urllib.parse import urlencode

from hypercorn.typing import ConnectionState
from quart.wrappers import Request
from werkzeug.datastructures import Headers

from discord_bot.webapi import ApiResult, extract_auth_token

if TYPE_CHECKING:
    from collections.abc import Mapping

    from hypercorn.typing import HTTPScope
    from quart import Quart, Response


class LocalTransport:
    def __init__(self, app: Quart) -> None:
        self.app = app

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object] | None = None,
        params: Mapping[str, str | int] | None = None,
        expect_image: bool = False,
    ) -> ApiResult:
        # Dispatch directly, without a socket, proxy middleware or a test client.
        # Production route decorators still enforce auth, fields and rate limits.
        from discord_bot.host import discord_user

        query = urlencode(params or {}).encode("ascii")
        request_headers = Headers(headers)
        request_headers["Host"] = "localhost"
        body = self.app.json.dumps(dict(json)).encode("utf-8") if json is not None else b""
        if json is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers["Content-Length"] = str(len(body))
        scope: HTTPScope = {
            "state": ConnectionState({}),
            "type": "http", "http_version": "1.1", "asgi": {"version": "3.0", "spec_version": "2.1"},
            "method": method, "scheme": "http", "path": path, "raw_path": path.encode("utf-8"),
            "query_string": query, "root_path": "", "headers": [],
            "client": (f"discord:{discord_user.get()}", 0), "server": ("localhost", 80), "extensions": {},
        }

        async def send_push_promise(path: str, headers: Headers) -> None:
            del path, headers

        local_request = Request(method, "http", path, query, request_headers, "", "1.1", scope, send_push_promise=send_push_promise)
        local_request.remote_addr = f"discord:{discord_user.get()}"
        local_request.body.set_result(body)
        async with self.app.request_context(local_request) as ctx:
            response = cast("Response", await self.app.make_response(await self.app.full_dispatch_request(ctx)))
            raw_headers = dict(response.headers)
            token = extract_auth_token(cast("Mapping[str, str]", response.headers))
            if expect_image and (response.mimetype or "").startswith("image/"):
                return ApiResult(True, response.status_code, None, None, raw_headers, cast("bytes", await response.get_data()), token)
            payload = cast("dict[str, object]", await response.get_json())
            msg = payload.get("msg")
            return ApiResult(
                bool(payload.get("success")) and 200 <= response.status_code < 300,
                response.status_code,
                msg if isinstance(msg, str) else None,
                payload.get("obj"),
                raw_headers,
                auth_token=token,
            )
