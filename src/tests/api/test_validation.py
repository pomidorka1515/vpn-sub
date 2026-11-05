from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from quart import g

from api.application import create_api_application
from api.decorators import requires_fields_strict
from util import ok

if TYPE_CHECKING:
    from api.common import ResponseType
    from custom_types import JsonifyValue

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    ("expected_type", "valid_value"),
    [(str, ""), (int, 0), (bool, False)],
)
@pytest.mark.parametrize("case", ["valid", "missing", "null", "wrong_type"])
async def test_required_typed_fields(
    expected_type: type[JsonifyValue],
    valid_value: JsonifyValue,
    case: str,
) -> None:
    app = create_api_application(proxy_hops=0)
    called = False

    @app.post("/test")
    @requires_fields_strict(("value", expected_type))
    async def handler() -> ResponseType:
        nonlocal called
        called = True
        return ok(obj=g.json_obj)

    body: dict[str, JsonifyValue] = (
        {}
        if case == "missing"
        else {
            "value": valid_value if case == "valid" else None if case == "null" else [],
        }
    )
    response = await app.test_client().post("/test", json=body)
    data = await response.get_json()
    assert called is (case == "valid")
    if case == "valid":
        assert response.status_code == 200
        assert data["obj"] == body
    else:
        assert response.status_code == 400
        assert data["success"] is False
        if case == "missing":
            assert data["msg"] == "Missing field: value"
        else:
            actual_type = "NoneType" if case == "null" else "list"
            assert data["msg"] == (
                f"Field 'value' must be {expected_type.__name__}, got {actual_type}"
            )


@pytest.mark.parametrize("value", [True, False, 1.9, 1.0, "1"])
async def test_strict_integer_rejects_other_json_types(value: JsonifyValue) -> None:
    app = create_api_application(proxy_hops=0)
    called = False

    @app.post("/test")
    @requires_fields_strict(("value", int))
    async def handler() -> ResponseType:
        nonlocal called
        called = True
        return ok()

    response = await app.test_client().post("/test", json={"value": value})
    assert response.status_code == 400
    assert not called
    assert (await response.get_json())["msg"] == (
        f"Field 'value' must be int, got {type(value).__name__}"
    )
