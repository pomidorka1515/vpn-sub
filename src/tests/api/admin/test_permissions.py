from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest

from admin_permissions import PERMISSIONS, ROUTE_PERMISSIONS, allows, validate_grants
from api import AsyncApi
from api.common import web_lang_tables

if TYPE_CHECKING:
    from quart import Quart
    from quart.typing import TestClientProtocol

    from config import Config, LangConfig
    from core import Subscription
    from custom_types import JsonifyValue

ROOT = {"Authorization": "secret"}
PREFIX = "/sub/api/api"


async def create_key(
    client: TestClientProtocol,
    permissions: list[str],
    name: str = "reader",
    headers: dict[str, str] | None = None,
) -> tuple[str, str]:
    response = await client.post(
        PREFIX + "/auth/create",
        headers=headers or ROOT,
        json={"kind": "tokens", "name": name, "permissions": permissions},
    )
    assert response.status_code == 201, await response.get_json()
    payload = (await response.get_json())["obj"]
    return payload["item"]["id"], payload["token"]


def test_closed_catalog_and_wildcard_boundaries() -> None:
    assert {route.handler for route in AsyncApi.ROUTES} == set(ROUTE_PERMISSIONS)
    assert allows(["*"], "future.action")
    assert allows(["user.edit.*"], "user.edit.displayname")
    assert not allows(["user.edit.*"], "user.read")
    assert not allows(["user.*"], "users.read")
    assert not allows(["user"], "user.read")
    assert validate_grants(["*", "user.read"]) == ["*"]
    assert validate_grants(["user.*", "user.read"]) == ["user.*"]
    assert len(PERMISSIONS) == len(set(PERMISSIONS))


def test_every_permission_has_localized_tooltips(language: Config[LangConfig]) -> None:
    for code in ("en", "ru"):
        _, strings, _ = web_lang_tables(language, "admin", code)
        for permission in PERMISSIONS:
            assert strings["permission_" + permission.replace(".", "_")]


@pytest.mark.parametrize(
    "grants",
    [
        ["user.edit.*"],
        ["code.add.*"],
        ["auth.create.accounts"],
        ["auth.update.tokens"],
        ["config.write.*"],
        ["user"],
        ["unknown.*"],
        [1],
        "*",
        None,
    ],
)
def test_invalid_grants_and_dependencies(grants: object) -> None:
    with pytest.raises(ValueError, match=r"permission|requires"):
        validate_grants(grants)


@pytest.mark.asyncio
async def test_keys_are_independent_hashed_and_revocable(
    admin_api: tuple[Quart, Subscription],
) -> None:
    app, sub = admin_api
    client = app.test_client()
    identity_id, token = await create_key(client, ["user.read"])
    headers = {"Authorization": "Bearer " + token}
    assert (await client.get(PREFIX + "/user/list", headers=headers)).status_code == 200
    denied = await client.get(PREFIX + "/logs/audit", headers=headers)
    assert denied.status_code == 403
    assert await denied.get_json() == {
        "success": False,
        "msg": "Missing audit.read permission",
        "obj": {"required_permission": "audit.read"},
    }
    listed = await client.get(PREFIX + "/auth/list?kind=tokens", headers=ROOT)
    assert token not in await listed.get_data(as_text=True)
    async with sub.res.db.connection() as conn:
        row = await conn.row("SELECT secret FROM admin_identities WHERE id = ?", (identity_id,))
        assert row is not None
        assert row[0] != token
    updated = await client.post(
        PREFIX + "/auth/update",
        headers=ROOT,
        json={
            "kind": "tokens",
            "id": identity_id,
            "name": "renamed",
            "permissions": ["audit.read"],
            "rotate": True,
        },
    )
    assert updated.status_code == 200
    new_token = (await updated.get_json())["obj"]["token"]
    assert (await client.get(PREFIX + "/user/list", headers=headers)).status_code == 401
    new_headers = {"Authorization": new_token}
    assert (await client.get(PREFIX + "/logs/audit", headers=new_headers)).status_code == 200
    assert (await client.get(PREFIX + "/user/list", headers=new_headers)).status_code == 403
    deleted = await client.post(
        PREFIX + "/auth/delete", headers=ROOT, json={"kind": "tokens", "id": identity_id}
    )
    assert deleted.status_code == 200
    assert (await client.get(PREFIX + "/logs/audit", headers=new_headers)).status_code == 401


@pytest.mark.asyncio
async def test_account_login_and_session_revocation(admin_api: tuple[Quart, Subscription]) -> None:
    app, sub = admin_api
    root = app.test_client()
    created = await root.post(
        PREFIX + "/auth/create",
        headers=ROOT,
        json={
            "kind": "accounts",
            "name": "operator",
            "password": "password",
            "permissions": ["code.read"],
        },
    )
    assert created.status_code == 201
    identity_id = (await created.get_json())["obj"]["item"]["id"]
    identity = await sub.res.db.admin_identity(identity_id)
    assert identity is not None
    assert identity.secret.startswith("$argon2id$")
    client = app.test_client()
    assert (
        await client.post("/sub/admin/session", json={"username": "operator", "password": "wrong"})
    ).status_code == 401
    assert (
        await client.post(
            "/sub/admin/session", json={"username": "operator", "password": "password"}
        )
    ).status_code == 200
    session = (await (await client.get("/sub/admin/token")).get_json())["obj"]
    assert session["permissions"] == ["code.read"]
    headers = {"Authorization": session["token"]}
    assert (await client.get(PREFIX + "/code/list", headers=headers)).status_code == 200
    assert (await client.get(PREFIX + "/user/list", headers=headers)).status_code == 403
    # Root and scoped browser sessions do not evict one another.
    assert (
        await root.post(
            "/sub/admin/session", json={"username": "admin", "password": "panel-secret"}
        )
    ).status_code == 200
    assert (await client.get("/sub/admin/token")).status_code == 200
    update = await root.post(
        PREFIX + "/auth/update",
        headers=ROOT,
        json={"kind": "accounts", "id": identity_id, "permissions": ["user.read"]},
    )
    assert update.status_code == 200
    assert (await client.get("/sub/admin/token")).status_code == 401
    assert (await client.get(PREFIX + "/code/list", headers=headers)).status_code == 401
    assert (await root.get("/sub/admin/token")).status_code == 200


@pytest.mark.asyncio
async def test_scoped_sessions_are_process_bound_and_logout_is_independent(
    admin_api: tuple[Quart, Subscription],
) -> None:
    app, _sub = admin_api
    client = app.test_client()
    created = await client.post(
        PREFIX + "/auth/create",
        headers=ROOT,
        json={
            "kind": "accounts",
            "name": "operator",
            "password": "password",
            "permissions": ["user.read"],
        },
    )
    assert created.status_code == 201
    assert (
        await client.post(
            "/sub/admin/session", json={"username": "operator", "password": "password"}
        )
    ).status_code == 200
    browser = (await (await client.get("/sub/admin/token")).get_json())["obj"]["token"]
    assert (await client.post("/sub/admin/logout")).status_code == 403
    assert (await client.get("/sub/admin/token")).status_code == 200
    assert (
        await client.post("/sub/admin/logout", headers={"X-Admin-CSRF": browser})
    ).status_code == 200
    assert (
        await client.get(PREFIX + "/user/list", headers={"Authorization": browser})
    ).status_code == 401
    assert (
        await client.post(
            "/sub/admin/session", json={"username": "operator", "password": "password"}
        )
    ).status_code == 200
    browser = (await (await client.get("/sub/admin/token")).get_json())["obj"]["token"]
    api = cast(AsyncApi, getattr(app.view_functions["admin_session"], "__self__"))
    api.ui_session_key = b"replacement-process-signing-key!"
    assert (await client.get("/sub/admin/token")).status_code == 401
    assert (
        await client.get(PREFIX + "/user/list", headers={"Authorization": browser})
    ).status_code == 401
    assert (await client.get(PREFIX + "/user/list", headers=ROOT)).status_code == 200


@pytest.mark.asyncio
async def test_no_delegation_or_takeover_above_own_scope(
    admin_api: tuple[Quart, Subscription],
) -> None:
    client = admin_api[0].test_client()
    _, manager = await create_key(
        client,
        ["auth.read.tokens", "auth.create.tokens", "auth.update.tokens", "user.read"],
        "manager",
    )
    powerful_id, _ = await create_key(client, ["*"], "powerful")
    headers = {"Authorization": manager}
    await create_key(client, ["user.read"], "delegated", headers)
    for permissions in (["*"], ["user.*"], ["audit.read"]):
        response = await client.post(
            PREFIX + "/auth/create",
            headers=headers,
            json={"kind": "tokens", "name": "invalid", "permissions": permissions},
        )
        assert response.status_code == 403
    for identity_id in (powerful_id, "root"):
        for action in ("update", "delete"):
            response = await client.post(
                PREFIX + "/auth/" + action,
                headers=headers,
                json={"kind": "tokens", "id": identity_id},
            )
            assert response.status_code == 403
    account = await client.post(
        PREFIX + "/auth/create",
        headers=headers,
        json={"kind": "accounts", "name": "denied", "password": "pass", "permissions": []},
    )
    assert account.status_code == 403
    for kind in ("accounts", "tokens"):
        response = await client.post(
            PREFIX + "/auth/delete", headers=ROOT, json={"kind": kind, "id": "root"}
        )
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_field_permissions_checked_before_mutation(
    admin_api: tuple[Quart, Subscription], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, sub = admin_api
    client = app.test_client()
    _, token = await create_key(client, ["user.read", "user.edit.displayname"])
    edit = AsyncMock(return_value=["displayname"])
    monkeypatch.setattr(sub.business_svc, "edit_user", edit)
    headers = {"Authorization": token}
    denied = await client.post(
        PREFIX + "/user/update",
        headers=headers,
        json={"username": "test", "displayname": "new", "limit": 1},
    )
    assert denied.status_code == 403
    assert (await denied.get_json())["obj"] == {
        "required_permission": "user.edit.bandwidth.monthly.limit"
    }
    edit.assert_not_awaited()
    allowed = await client.post(
        PREFIX + "/user/update", headers=headers, json={"username": "test", "displayname": "new"}
    )
    assert allowed.status_code == 200
    edit.assert_awaited_once_with(username="test", displayname="new")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body", "permission"),
    [
        ("/leaderboard", {"type": "monthly"}, "leaderboard.read.monthly"),
        ("/code/add", {"action": "register"}, "code.add.register"),
        ("/config/set", {"values": {"3xui": {}}}, "config.write.panels"),
        ("/config/set", {"values": {"api_admin_ui_auth": ["new", "password"]}}, "*"),
        ("/operations/rollback/resolve", {}, "*"),
    ],
)
async def test_dynamic_permission_checks(
    admin_api: tuple[Quart, Subscription], path: str, body: dict[str, JsonifyValue], permission: str
) -> None:
    client = admin_api[0].test_client()
    _, token = await create_key(
        client,
        [
            "leaderboard.read.total",
            "code.read",
            "code.add.bonus",
            "config.read.main",
            "config.write.main",
            "health.read.recovery",
        ],
    )
    response = await client.post(PREFIX + path, headers={"Authorization": token}, json=body)
    assert response.status_code == 403
    assert (await response.get_json())["obj"] == {"required_permission": permission}


@pytest.mark.asyncio
async def test_scoped_config_redacts_root_and_other_sections(
    admin_api: tuple[Quart, Subscription],
) -> None:
    client = admin_api[0].test_client()
    _, token = await create_key(client, ["config.read.inbounds"])
    response = await client.get(PREFIX + "/config/get", headers={"Authorization": token})
    assert response.status_code == 200
    assert set((await response.get_json())["obj"]) == {"profiles", "nodes"}
    _, main = await create_key(client, ["config.read.main"], "main")
    payload = (
        await (await client.get(PREFIX + "/config/get", headers={"Authorization": main})).get_json()
    )["obj"]
    assert not (
        {"api_token", "api_admin_ui_auth", "salt", "bot", "discord", "redis", "profiles", "3xui"}
        & payload.keys()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"kind": []},
        {"kind": "tokens", "permissions": ["unknown.read"]},
        {"kind": "tokens", "permissions": ["user.edit.*"]},
        {"kind": "tokens", "permissions": [], "rotate": "true"},
        {"kind": "tokens", "permissions": [], "secret": "injected"},
        {"kind": "accounts", "permissions": [], "password": ""},
        {"kind": "accounts", "permissions": [], "password": "a" * 129},
    ],
)
async def test_strict_management_validation(
    admin_api: tuple[Quart, Subscription], body: dict[str, JsonifyValue]
) -> None:
    response = (
        await admin_api[0]
        .test_client()
        .post(PREFIX + "/auth/create", headers=ROOT, json={"name": "test", **body})
    )
    assert response.status_code == 400
