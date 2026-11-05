from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from api.application import create_api_application
from app_runtime.tasks import CapacityExceededError, MutationRegistry, TaskRegistry
from util import ok

if TYPE_CHECKING:
    from api.common import ResponseType

pytestmark = pytest.mark.asyncio


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
    error_type: type[RuntimeError], caplog: pytest.LogCaptureFixture,
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
