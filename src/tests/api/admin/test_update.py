from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from errors import NotFoundError
from tests.support.users import create_alice

if TYPE_CHECKING:
    from quart import Quart

    from core import Subscription
    from db import Database

pytestmark = pytest.mark.asyncio

URL = "/sub/api/api/user/update"
HEADERS = {"Authorization": "secret"}


async def test_update_every_field_atomically(
    admin_api: tuple[Quart, Subscription], database: Database
) -> None:
    app, sub = admin_api
    await create_alice(database, ext_username="alice-login", ext_password_hash="unchanged")
    await database.add_bandwidth_snapshot("alice", 123, 10, 20, 30, 40)
    history = await database.get_bandwidth_snapshots("alice", 0)
    before = await sub.user_svc.user("alice")
    audit = AsyncMock()
    config = sub.business_svc.cfg.view()
    config["fingerprints"] = ["chrome", "firefox"]
    with (
        patch.object(sub.audit_svc, "audit", audit),
        patch.object(sub.business_svc.cfg, "view", return_value=config),
        patch.object(sub.business_svc, "_post", AsyncMock()) as panel_write,
    ):
        response = await app.test_client().post(
            URL,
            headers=HEADERS,
            json={
                "username": "alice-login",
                "new_username": "renamed",
                "displayname": "Updated",
                "fingerprint": "firefox",
                "time": 1234567890,
                "limit": 10,
                "used": 123456789,
                "wl_limit": 20,
                "wl_used": 987654321,
            },
        )
    assert response.status_code == 200
    assert (await response.get_json())["obj"] == {
        "updated": [
            "username",
            "displayname",
            "fingerprint",
            "time",
            "limit",
            "used",
            "wl_limit",
            "wl_used",
        ]
    }
    after = await sub.user_svc.user("alice")
    expected = {
        **before,
        "fingerprint": "firefox",
        "ext_username": "renamed",
        "displayname": "Updated",
        "expires_at": 1234567890,
        "bw_limit_gb": 10,
        "bw_used": 123456789,
        "wl_limit_gb": 20,
        "wl_used": 987654321,
    }
    assert after == expected
    assert await database.get_bandwidth_snapshots("alice", 0) == history
    panel_write.assert_not_awaited()
    assert await database.ext_to_user("alice-login") is None
    assert await database.ext_to_user("renamed") == "alice"
    assert audit.await_args is not None
    assert audit.await_args.kwargs["name"] == "user_edit"


@pytest.mark.parametrize(
    "changes",
    [
        {"password": "secret"},
        {"ext_password": "secret"},
        {"token": "new"},
        {"uuid": "new"},
        {"all_time": 0},
        {"user": "alice"},
        {"new_username": None},
        {"new_username": "!!!"},
        {"new_username": "a" * 33},
        {"displayname": None},
        {"displayname": "a" * 17},
        {"fingerprint": None},
        {"fingerprint": "unknown"},
        {"time": -1},
        {"used": None},
        {"used": True},
        {"used": -1},
        {"used": 1.5},
        {"used": 2**63},
        {"wl_used": 2**63},
    ],
)
async def test_invalid_update_leaves_everything_unchanged(
    admin_api: tuple[Quart, Subscription], database: Database, changes: dict[str, object]
) -> None:
    app, sub = admin_api
    await create_alice(database, ext_username="alice-login")
    before = await sub.user_svc.user("alice")
    response = await app.test_client().post(
        URL,
        headers=HEADERS,
        json={
            "username": "alice-login",
            "displayname": "Updated",
            "new_username": "renamed",
            **changes,
        },
    )
    assert response.status_code == 400
    assert await sub.user_svc.user("alice") == before


async def test_duplicate_rename_rolls_back_other_fields(
    admin_api: tuple[Quart, Subscription], database: Database
) -> None:
    app, sub = admin_api
    await create_alice(database, ext_username="alice-login")
    await create_alice(
        database, username="bob", ext_username="bob-login", uuid="bob", token="b" * 40
    )
    before = await sub.user_svc.user("alice")
    response = await app.test_client().post(
        URL,
        headers=HEADERS,
        json={"username": "alice-login", "new_username": "bob-login", "used": 123, "limit": 4},
    )
    assert response.status_code == 409
    assert await sub.user_svc.user("alice") == before


@pytest.mark.parametrize("username", ["alice", "missing", ""])
async def test_update_requires_external_username(
    admin_api: tuple[Quart, Subscription], database: Database, username: str
) -> None:
    await create_alice(database, ext_username="alice-login")
    response = (
        await admin_api[0]
        .test_client()
        .post(URL, headers=HEADERS, json={"username": username, "used": 0})
    )
    assert response.status_code == 404


async def test_noop_and_zero_values(
    admin_api: tuple[Quart, Subscription], database: Database
) -> None:
    app, sub = admin_api
    await create_alice(
        database, ext_username="alice-login", expires_at=123, bw_limit_gb=10, wl_limit_gb=5
    )
    await database.update_user("alice", bw_used=100, wl_bw_used=200)
    client = app.test_client()
    response = await client.post(URL, headers=HEADERS, json={"username": "alice-login"})
    assert response.status_code == 200
    assert (await response.get_json())["obj"] == {"updated": []}
    response = await client.post(
        URL,
        headers=HEADERS,
        json={
            "username": "alice-login",
            "time": 0,
            "limit": 0,
            "used": 0,
            "wl_limit": 0,
            "wl_used": 0,
        },
    )
    assert response.status_code == 200
    user = await sub.user_svc.user("alice")
    assert all(
        user[key] == 0 for key in ("expires_at", "bw_limit_gb", "bw_used", "wl_limit_gb", "wl_used")
    )


async def test_audit_failure_preserves_committed_update_result(
    admin_api: tuple[Quart, Subscription], database: Database
) -> None:
    app, sub = admin_api
    await create_alice(database, ext_username="alice-login")
    with patch.object(sub.audit_svc, "audit", AsyncMock(side_effect=OSError)):
        response = await app.test_client().post(
            URL,
            headers=HEADERS,
            json={"username": "alice-login", "new_username": "renamed", "used": 0},
        )
    assert response.status_code == 202
    assert (await response.get_json())["obj"] == {
        "committed": True,
        "audit_pending": True,
        "updated": ["username", "used"],
    }
    assert await database.ext_to_user("renamed") == "alice"


async def test_stale_resolution_cannot_edit_after_rename(
    admin_api: tuple[Quart, Subscription], database: Database
) -> None:
    _, sub = admin_api
    await create_alice(database, ext_username="alice-login")
    resolved = asyncio.Event()
    held = asyncio.Event()
    original = database.ext_to_user

    async def resolve(username: str) -> str | None:
        internal = await original(username)
        resolved.set()
        return internal

    async def rename() -> None:
        held.set()
        await resolved.wait()
        await database.update_user("alice", ext_username="renamed")

    with patch.object(database, "ext_to_user", resolve):
        owner = asyncio.create_task(sub.res.coordinator.run(["alice"], rename, name="test:rename"))
        await held.wait()
        with pytest.raises(NotFoundError):
            await sub.business_svc.update_external_user("alice-login", used=99)
        await owner
    assert (await sub.user_svc.user("alice"))["bw_used"] == 0


@pytest.mark.parametrize("headers", [{}, {"Authorization": "wrong"}])
async def test_update_requires_admin(
    admin_api: tuple[Quart, Subscription], headers: dict[str, str]
) -> None:
    response = (
        await admin_api[0]
        .test_client()
        .post(URL, headers=headers, json={"username": "alice-login", "used": 0})
    )
    assert response.status_code == 401


async def test_recovery_blocks_edit(
    admin_api: tuple[Quart, Subscription], database: Database
) -> None:
    app, sub = admin_api
    await create_alice(database, ext_username="alice-login")
    await database.set_metadata("user_reset_pending:alice", "pending")
    before = await sub.user_svc.user("alice")
    response = await app.test_client().post(
        URL,
        headers=HEADERS,
        json={"username": "alice-login", "new_username": "renamed", "used": 5},
    )
    assert response.status_code == 409
    assert await sub.user_svc.user("alice") == before
