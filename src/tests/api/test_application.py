from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from quart import Response
from werkzeug.datastructures import WWWAuthenticate
from werkzeug.exceptions import TooManyRequests, Unauthorized
from werkzeug.wrappers import Response as WerkzeugResponse

from api.application import create_api_application
from app_runtime.tasks import CapacityExceededError, MutationRegistry, TaskRegistry
from util import ok

if TYPE_CHECKING:
    from werkzeug.exceptions import HTTPException

    from api.common import ResponseType

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    ("path", "headers", "status", "mimetype"),
    [
        ("/html", {}, 200, "text/html"),
        ("/json", {}, 200, "application/json"),
        ("/asset", {}, 200, "text/javascript"),
        ("/missing", {}, 404, "application/json"),
        ("/missing", {"User-Agent": "Firefox/120.0"}, 404, "text/html"),
        ("/failure", {}, 500, "application/json"),
    ],
)
async def test_security_headers_on_responses(
    path: str, headers: dict[str, str], status: int, mimetype: str
) -> None:
    app = create_api_application(proxy_hops=0)

    @app.get("/html")
    async def html() -> Response:
        return Response(
            "<html></html>", mimetype="text/html", headers={"X-Frame-Options": "SAMEORIGIN"}
        )

    @app.get("/json")
    async def json_response() -> ResponseType:
        return ok()

    @app.get("/asset")
    async def asset() -> Response:
        return Response("", mimetype="text/javascript")

    @app.get("/failure")
    async def failure() -> ResponseType:
        raise RuntimeError("failure")

    response = await app.test_client().get(path, headers=headers)
    assert response.status_code == status
    assert response.mimetype == mimetype
    assert response.headers.getlist("X-Frame-Options") == ["DENY"]
    assert response.headers.getlist("X-Content-Type-Options") == ["nosniff"]
    csp = response.headers.getlist("Content-Security-Policy")
    assert len(csp) == 1
    assert "script-src 'self'" in csp[0]
    assert "style-src 'self' 'unsafe-inline'" in csp[0]
    assert "img-src 'self' blob:" in csp[0]
    assert "frame-ancestors 'none'" in csp[0]


async def test_security_headers_on_proxy_rejection() -> None:
    app = create_api_application(require_proxy=True)
    response = await app.test_client().get("/", scope_base={"client": ("10.0.0.1", 1234)})
    assert response.status_code == 400
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "img-src 'self' blob:" in response.headers["Content-Security-Policy"]


async def test_method_not_allowed_preserves_allow_header() -> None:
    app = create_api_application(proxy_hops=0)

    @app.get("/work")
    async def work() -> ResponseType:
        return ok()

    response = await app.test_client().post("/work")

    assert response.status_code == 405
    assert set(response.headers["Allow"].split(", ")) == {"GET", "HEAD", "OPTIONS"}
    assert response.mimetype == "application/json"
    assert (await response.get_json())["success"] is False
    assert response.headers["Content-Length"] == str(len(await response.get_data()))


@pytest.mark.parametrize(
    ("error", "header", "values"),
    [
        (TooManyRequests(retry_after=60), "Retry-After", ["60"]),
        (
            Unauthorized(www_authenticate=[WWWAuthenticate("Basic"), WWWAuthenticate("Bearer")]),
            "WWW-Authenticate",
            ["Basic", "Bearer"],
        ),
        (
            TooManyRequests(
                response=WerkzeugResponse(
                    "old body",
                    status=429,
                    content_type="text/plain",
                    headers={"X-Error": "original"},
                )
            ),
            "X-Error",
            ["original"],
        ),
    ],
)
async def test_http_exception_preserves_headers_with_json_body(
    error: HTTPException,
    header: str,
    values: list[str],
) -> None:
    app = create_api_application(proxy_hops=0)

    @app.get("/work")
    async def work() -> ResponseType:
        raise error

    response = await app.test_client().get("/work")

    assert response.status_code == (error.code or 500)
    assert response.headers.getlist(header) == values
    assert response.headers.getlist("Content-Type") == ["application/json"]
    assert response.headers.getlist("Content-Length") == [str(len(await response.get_data()))]
    assert (await response.get_json())["success"] is False


@pytest.mark.parametrize("owner", ["mutation", "panel", "config"])
async def test_capacity_exhaustion_is_retryable_and_does_not_submit(owner: str) -> None:
    app = create_api_application(proxy_hops=0)
    registry = MutationRegistry(capacity=1) if owner == "mutation" else TaskRegistry(capacity=1)
    release = asyncio.Event()
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1

    @app.post("/work")
    async def work() -> ResponseType:
        if isinstance(registry, MutationRegistry):
            await registry.run(operation, name=owner)
        else:
            await registry.create(operation, name=owner)
        return ok()

    occupied = registry.create(release.wait, name="occupy-capacity")
    try:
        response = await app.test_client().post("/work")
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "1"
        data = await response.get_json()
        assert data["success"] is False
        assert data["msg"] == "Service temporarily busy. Retry after 1 second."
        assert calls == 0
        assert registry.active_count == 1
        release.set()
        await occupied
        response = await app.test_client().post("/work")
        assert response.status_code == 200
        assert "Retry-After" not in response.headers
        assert calls == 1
    finally:
        release.set()
        await registry.drain()


@pytest.mark.parametrize("error_type", [CapacityExceededError, RuntimeError])
async def test_error_responses_do_not_expose_exception_details(
    error_type: type[RuntimeError],
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_api_application(proxy_hops=0)

    @app.post("/work")
    async def work() -> ResponseType:
        raise error_type("credential-secret")

    response = await app.test_client().post("/work?token=query-secret")
    assert response.status_code == (503 if error_type is CapacityExceededError else 500)
    if error_type is RuntimeError:
        assert (await response.get_json())["msg"] == "Internal server error"
        assert "Retry-After" not in response.headers
    assert "credential-secret" not in await response.get_data(as_text=True)
    assert "credential-secret" not in caplog.text
    assert "query-secret" not in caplog.text
