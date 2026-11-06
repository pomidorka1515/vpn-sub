from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from core.services.refresh import RefreshResult
from errors import AppError, PanelUnavailableError

if TYPE_CHECKING:
    from quart import Quart

    from core import Subscription

pytestmark = pytest.mark.asyncio


async def test_user_refresh_requires_post(
    admin_api: tuple[Quart, Subscription],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, sub = admin_api
    refresh = AsyncMock(return_value=RefreshResult([], None, 1, 1))
    monkeypatch.setattr(sub.refresh_svc, "refresh", refresh)
    client = app.test_client()
    for send in (client.get, client.head):
        response = await send(
            "/sub/api/api/user/refresh",
            headers={"Authorization": "secret"},
        )
        assert response.status_code == 405
        refresh.assert_not_called()

    response = await client.post(
        "/sub/api/api/user/refresh",
        headers={"Authorization": "secret"},
    )
    assert response.status_code == 200
    assert (await response.get_json())["msg"] == "Refreshed all users."
    refresh.assert_called_once_with()
    refresh.assert_awaited_once_with()


async def test_user_refresh_aborts_on_non_panel_error(
    admin_api: tuple[Quart, Subscription],
) -> None:
    app, sub = admin_api
    for name in ("alice", "bob"):
        await sub.res.db.create_user(
            username=name,
            uuid=name,
            token=name,
            fingerprint="chrome",
            displayname=name,
        )

    async def add_users(
        username: str,
        _called_internally: bool = False,
        *,
        work: str = "foreground",
    ) -> None:
        if username == "bob":
            raise AppError("panel rejected")

    sub.business_svc.add_users = add_users  # type: ignore[method-assign]
    response = await app.test_client().post(
        "/sub/api/api/user/refresh",
        headers={"Authorization": "secret"},
    )
    assert response.status_code == 500
    payload = await response.get_json()
    assert payload["msg"] == "Refresh aborted"
    assert payload["obj"] == {"aborted": "bob", "failed": [], "succeeded": 1, "total": 2}


@pytest.mark.parametrize("user_count", [0, 1, 9])
async def test_user_refresh_uses_bounded_workers(
    admin_api: tuple[Quart, Subscription],
    monkeypatch: pytest.MonkeyPatch,
    user_count: int,
) -> None:
    app, sub = admin_api
    users = [f"user{index}" for index in range(user_count)]
    started: list[str] = []
    active = 0
    peak = 0
    saturated = asyncio.Event()
    release = asyncio.Event()

    async def list_users() -> list[str]:
        return users

    async def add_users(username: str) -> None:
        nonlocal active, peak
        started.append(username)
        active += 1
        peak = max(peak, active)
        if active == min(4, user_count):
            saturated.set()
        try:
            await release.wait()
        finally:
            active -= 1

    monkeypatch.setattr(sub.user_svc, "list_users", list_users)
    monkeypatch.setattr(sub.business_svc, "add_users", add_users)
    refresh = asyncio.create_task(
        app.test_client().post(
            "/sub/api/api/user/refresh",
            headers={"Authorization": "secret"},
        ),
    )
    try:
        if users:
            async with asyncio.timeout(5):
                await saturated.wait()
            assert started == users[:4]
            workers = [
                task
                for task in asyncio.all_tasks()
                if task.get_name().startswith("core:user-refresh-worker:")
            ]
            assert len(workers) == min(4, user_count)
        release.set()
        async with asyncio.timeout(5):
            response = await refresh
        assert response.status_code == 200
        assert (await response.get_json())["msg"] == "Refreshed all users."
        assert sorted(started) == users
        assert peak == min(4, user_count)
    finally:
        release.set()
        await refresh


@pytest.mark.parametrize("all_failed", [False, True])
async def test_user_refresh_reports_panel_failures_in_user_order(
    admin_api: tuple[Quart, Subscription],
    monkeypatch: pytest.MonkeyPatch,
    all_failed: bool,
) -> None:
    app, sub = admin_api
    users = [f"user{index}" for index in range(9)]
    first_failed = asyncio.Event()

    async def list_users() -> list[str]:
        return users

    async def add_users(username: str) -> None:
        if username == "user0":
            await first_failed.wait()
        if all_failed or username in {"user0", "user1"}:
            first_failed.set()
            raise PanelUnavailableError("offline")

    monkeypatch.setattr(sub.user_svc, "list_users", list_users)
    monkeypatch.setattr(sub.business_svc, "add_users", add_users)
    async with asyncio.timeout(5):
        response = await app.test_client().post(
            "/sub/api/api/user/refresh",
            headers={"Authorization": "secret"},
        )
    assert response.status_code == (502 if all_failed else 200)
    payload = await response.get_json()
    if all_failed:
        assert payload["obj"] == {"failed": users, "total": 9}
    else:
        assert payload["obj"] == {"failed": users[:2], "succeeded": 7, "total": 9}


async def test_user_refresh_abort_drains_in_flight_and_stops_new_users(
    admin_api: tuple[Quart, Subscription],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, sub = admin_api
    users = ["alice", "bob", "carol", "dave", "eve", "frank"]
    started: list[str] = []
    finished: list[str] = []
    saturated = asyncio.Event()
    aborted = asyncio.Event()
    release = asyncio.Event()

    async def list_users() -> list[str]:
        return users

    async def add_users(username: str) -> None:
        started.append(username)
        if len(started) == 4:
            saturated.set()
        try:
            if username == "bob":
                await saturated.wait()
                aborted.set()
                raise AppError("unexpected failure")
            await release.wait()
            if username == "alice":
                raise PanelUnavailableError("offline")
            if username == "carol":
                raise AppError("another in-flight failure")
        finally:
            finished.append(username)

    monkeypatch.setattr(sub.user_svc, "list_users", list_users)
    monkeypatch.setattr(sub.business_svc, "add_users", add_users)
    refresh = asyncio.create_task(
        app.test_client().post(
            "/sub/api/api/user/refresh",
            headers={"Authorization": "secret"},
        ),
    )
    try:
        async with asyncio.timeout(5):
            await aborted.wait()
        assert not refresh.done()
        assert finished == ["bob"]
        release.set()
        async with asyncio.timeout(5):
            response = await refresh
        assert response.status_code == 500
        payload = await response.get_json()
        assert payload["msg"] == "Refresh aborted"
        assert payload["obj"] == {
            "aborted": "bob",
            "failed": ["alice"],
            "succeeded": 1,
            "total": 6,
        }
        assert started == users[:4]
        assert sorted(finished) == users[:4]
    finally:
        release.set()
        await refresh
