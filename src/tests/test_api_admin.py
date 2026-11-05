from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

if TYPE_CHECKING:
    from quart import Quart

    from core import Subscription

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("resource", ["user", "code"])
@pytest.mark.parametrize("agent", ["Mozilla/5.0 Chrome/130.0 Safari/537.36", "curl/8.0"])
async def test_missing_admin_resource_preserves_json(
    admin_api: tuple[Quart, Subscription],
    resource: str,
    agent: str,
) -> None:
    client = admin_api[0].test_client()
    response = await client.get(
        f"/sub/api/api/{resource}/info",
        query_string={resource: "missing"},
        headers={"Authorization": "secret", "User-Agent": agent},
    )
    assert response.status_code == 404
    assert response.mimetype == "application/json"
    data = await response.get_json()
    assert data["success"] is False
    assert data["msg"]


@pytest.mark.parametrize("exists", [True, False])
async def test_panel_status_failure_preserves_missing_panel_response(
    admin_api: tuple[Quart, Subscription],
    monkeypatch: pytest.MonkeyPatch,
    exists: bool,
) -> None:
    app, sub = admin_api
    getstatus = AsyncMock(return_value=None)
    monkeypatch.setattr(sub.panel_svc, "getstatus", getstatus)
    name = sub.panels[0].name if exists else "missing"
    response = await app.test_client().get(
        "/sub/api/api/panel/status",
        query_string={"name": name},
        headers={"Authorization": "secret"},
    )
    assert response.status_code == (502 if exists else 404)
    payload = await response.get_json()
    assert payload["success"] is False
    assert payload["msg"] == (
        "getstatus() returned None; panel may be down" if exists else "panel not found"
    )
    if exists:
        getstatus.assert_awaited_once_with(sub.panels[0])
    else:
        getstatus.assert_not_awaited()


@pytest.mark.parametrize("perma", [False, True, "false"])
async def test_delete_rejects_retired_flag(
    admin_api: tuple[Quart, Subscription],
    perma: bool | str,
) -> None:
    app, sub = admin_api
    delete = AsyncMock()
    sub.business_svc.delete_user = delete  # type: ignore[method-assign]
    response = await app.test_client().post(
        "/sub/api/api/user/delete",
        headers={"Authorization": "secret"},
        json={"user": "alice", "perma": perma},
    )
    assert response.status_code == 400
    delete.assert_not_awaited()


async def test_delete_calls_permanent_service_without_flag(
    admin_api: tuple[Quart, Subscription],
) -> None:
    app, sub = admin_api
    delete = AsyncMock()
    sub.business_svc.delete_user = delete  # type: ignore[method-assign]
    response = await app.test_client().post(
        "/sub/api/api/user/delete",
        headers={"Authorization": "secret"},
        json={"user": "alice"},
    )
    assert response.status_code == 200
    delete.assert_awaited_once_with("alice")
