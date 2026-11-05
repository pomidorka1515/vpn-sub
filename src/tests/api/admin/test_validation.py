from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

if TYPE_CHECKING:
    from quart import Quart

    from core import Subscription

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("operation", ["add", "update"])
@pytest.mark.parametrize("field", ["limit", "wl_limit", "time"])
@pytest.mark.parametrize(
    "value",
    [
        float("inf"),
        float("-inf"),
        float("nan"),
        2**70,
        str(2**70),
        2**63 - 1,
        1.9,
        -0.5,
        True,
        False,
        "1.9",
    ],
)
async def test_user_numeric_bounds(
    admin_api: tuple[Quart, Subscription],
    operation: str,
    field: str,
    value: float | str,
) -> None:
    app, sub = admin_api
    add = AsyncMock()
    update = AsyncMock()
    sub.business_svc.add_new_user = add  # type: ignore[method-assign]
    sub.business_svc.update_params = update  # type: ignore[method-assign]
    response = await app.test_client().post(
        f"/sub/api/api/user/{operation}",
        headers={"Authorization": "secret"},
        json={"user": "alice", "displayname": "Alice", field: value},
    )
    assert response.status_code == 400
    message = (await response.get_json())["msg"]
    assert field in message
    assert "range" in message or "finite integer" in message
    add.assert_not_awaited()
    update.assert_not_awaited()


@pytest.mark.parametrize("operation", ["add", "update"])
@pytest.mark.parametrize("field", ["limit", "wl_limit", "time"])
async def test_user_numeric_boundary(
    admin_api: tuple[Quart, Subscription],
    operation: str,
    field: str,
) -> None:
    app, sub = admin_api
    mutation = AsyncMock()
    if operation == "add":
        sub.business_svc.add_new_user = mutation  # type: ignore[method-assign]
    else:
        sub.business_svc.update_params = mutation  # type: ignore[method-assign]
    maximum = 2**31 if field == "time" else (2**63 - 1) // 10**9
    for value, status in ((maximum + 1, 400), (maximum, 201 if operation == "add" else 200)):
        response = await app.test_client().post(
            f"/sub/api/api/user/{operation}",
            headers={"Authorization": "secret"},
            json={"user": "alice", "displayname": "Alice", field: value},
        )
        assert response.status_code == status
    mutation.assert_awaited_once()
    assert mutation.await_args is not None
    assert mutation.await_args.kwargs["timee" if field == "time" else field] == maximum


@pytest.mark.parametrize("field", ["gb", "wl_gb", "uses"])
async def test_code_numeric_boundary(
    admin_api: tuple[Quart, Subscription],
    field: str,
) -> None:
    maximum = 2**63 - 1 if field == "uses" else (2**63 - 1) // 10**9
    response = (
        await admin_api[0]
        .test_client()
        .post(
            "/sub/api/api/code/add",
            headers={"Authorization": "secret"},
            json={"code": "invite", "action": "register", field: maximum},
        )
    )
    assert response.status_code == 201


@pytest.mark.parametrize("field", ["days", "gb", "wl_gb", "uses"])
@pytest.mark.parametrize(
    "value",
    [
        float("inf"),
        float("-inf"),
        float("nan"),
        2**70,
        str(2**70),
        1.9,
        -0.5,
        True,
        False,
        "1.9",
    ],
)
async def test_code_numeric_bounds(
    admin_api: tuple[Quart, Subscription],
    field: str,
    value: float | str,
) -> None:
    app, sub = admin_api
    add = AsyncMock()
    sub.code_svc.add_code = add  # type: ignore[method-assign]
    response = await app.test_client().post(
        "/sub/api/api/code/add",
        headers={"Authorization": "secret"},
        json={"code": "invite", "action": "register", field: value},
    )
    assert response.status_code == 400
    message = (await response.get_json())["msg"]
    assert field in message
    assert "range" in message or "finite integer" in message
    add.assert_not_awaited()


@pytest.mark.parametrize("value", [0, 1, 1.0, "1", " +1 "])
async def test_admin_integer_representations(
    admin_api: tuple[Quart, Subscription],
    value: float | str,
) -> None:
    app, sub = admin_api
    add_user = AsyncMock()
    update_user = AsyncMock()
    add_code = AsyncMock()
    sub.business_svc.add_new_user = add_user  # type: ignore[method-assign]
    sub.business_svc.update_params = update_user  # type: ignore[method-assign]
    sub.code_svc.add_code = add_code  # type: ignore[method-assign]
    for operation, mutation, fields, body, status in (
        (
            "user/add",
            add_user,
            ("limit", "wl_limit", "time"),
            {"user": "alice", "displayname": "Alice"},
            201,
        ),
        ("user/update", update_user, ("limit", "wl_limit", "time"), {"user": "alice"}, 200),
        (
            "code/add",
            add_code,
            ("days", "gb", "wl_gb", "uses"),
            {"code": "invite", "action": "register"},
            201,
        ),
    ):
        response = await app.test_client().post(
            f"/sub/api/api/{operation}",
            headers={"Authorization": "secret"},
            json={**body, **dict.fromkeys(fields, value)},
        )
        assert response.status_code == status
        mutation.assert_awaited_once()
        assert mutation.await_args is not None
        for field in fields:
            result = mutation.await_args.kwargs["timee" if field == "time" else field]
            assert type(result) is int
            assert result == int(value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("gb", (2**63 - 1) // 10**9 + 1),
        ("wl_gb", (2**63 - 1) // 10**9 + 1),
        ("days", (2**63 - 1) // 86400),
    ],
)
async def test_code_numeric_unit_overflow(
    admin_api: tuple[Quart, Subscription],
    field: str,
    value: int,
) -> None:
    response = (
        await admin_api[0]
        .test_client()
        .post(
            "/sub/api/api/code/add",
            headers={"Authorization": "secret"},
            json={"code": "invite", "action": "register", field: value},
        )
    )
    assert response.status_code == 400
    assert "range" in (await response.get_json())["msg"]
