from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from core.services.updates import UpdateSnapshot
from errors import PanelUnavailableError
from tests.support.panels import FakePanel, make_panel_client
from tests.support.subscriptions import make_subscription

if TYPE_CHECKING:
    from config import JsonValue
    from custom_types import UpdateUser
    from db import Database

pytestmark = pytest.mark.asyncio


async def test_late_viewer_gets_new_batch_and_viewers_are_reference_counted(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("alice", "bob"):
        await database.create_user(
            username=name,
            uuid=f"{name}-id",
            token=f"{name}-token",
            fingerprint="chrome",
            displayname=name,
        )
    sub = await make_subscription(database)
    entered, release = asyncio.Event(), asyncio.Event()
    original = database.list_update_users
    batches: list[frozenset[str]] = []

    async def load(names: frozenset[str]) -> list[UpdateUser]:
        batches.append(names)
        if len(batches) == 1:
            entered.set()
            await release.wait()
        return await original(names)

    monkeypatch.setattr(database, "list_update_users", load)
    sub.update_svc.add_viewer("alice")
    sub.update_svc.add_viewer("alice")
    first = asyncio.create_task(sub.update_svc.snapshot("alice"))
    await entered.wait()
    sub.update_svc.add_viewer("bob")
    late = asyncio.create_task(sub.update_svc.snapshot("bob"))
    release.set()
    alice, bob = await asyncio.gather(first, late)
    assert set(alice.users) == {"alice"}
    assert set(bob.users) == {"alice", "bob"}
    assert batches == [frozenset({"alice"}), frozenset({"alice", "bob"})]
    sub.update_svc.remove_viewer("alice")
    assert sub.update_svc._viewers["alice"] == 1
    sub.update_svc.remove_viewer("alice")
    sub.update_svc.remove_viewer("bob")
    assert not sub.update_svc._viewers


async def test_updates_batch_is_shared_and_identity_filtered(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await database.create_user(
        username="alice",
        uuid="alice-id",
        token="alice-token",
        fingerprint="chrome",
        displayname="Alice",
        bw_limit_gb=10,
        wl_limit_gb=20,
        expires_at=123,
    )
    await database.create_user(
        username="bob",
        uuid="bob-id",
        token="bob-token",
        fingerprint="firefox",
        displayname="Bob",
    )
    await database.set_auth_token("alice", "a" * 100)
    await database.increment_usage("alice", regular=1234, whitelist=5678)
    panel = FakePanel(
        clients=[make_panel_client("alice", [1], 10, 20, uuid="alice-id")],
        post_payload={"success": True, "obj": ["alice"]},
    )
    wl = FakePanel(name="wl", clients=[make_panel_client("alice", [1], 30, 40, uuid="alice-id")])
    sub = await make_subscription(database, panels=[panel], whitelist_panel=wl)
    sub.update_svc.add_viewer("alice")
    records = AsyncMock(wraps=database.list_update_users)
    monkeypatch.setattr(database, "list_update_users", records)
    usernames = AsyncMock(wraps=database.usernames)
    monkeypatch.setattr(database, "usernames", usernames)
    clients = AsyncMock(wraps=sub.panel_svc.traffic_snapshot)
    monkeypatch.setattr(sub.panel_svc, "traffic_snapshot", clients)
    clock = [0.0]
    monkeypatch.setattr("core.services.updates.time", Mock(wraps=time, monotonic=lambda: clock[0]))

    snapshots = await asyncio.gather(*(sub.update_svc.snapshot("alice") for _ in range(100)))
    assert all(snapshot is snapshots[0] for snapshot in snapshots)
    assert records.await_count == 1
    assert records.await_args is not None
    assert records.await_args.args[0] == frozenset({"alice"})
    assert "bob" not in snapshots[0].users
    assert usernames.await_count == 0
    assert clients.await_count == 2  # One call per panel, not per viewer.
    snapshot = snapshots[0]
    assert "ext_password_hash" not in snapshot.users["alice"]
    assert "created_at" not in snapshot.users["alice"]
    assert snapshot.frames == {}
    assert snapshot.authorized_frame("alice", "wrong") is None
    assert snapshot.frames == {}
    frame = snapshot.authorized_frame("alice", "a" * 100)
    assert frame is not None
    assert snapshot.authorized_frame("alice", "a" * 100) is frame
    assert set(snapshot.frames) == {"alice"}
    info = json.loads(frame.removeprefix(b"data: "))
    assert info["displayname"] == "Alice"
    assert info["time"] == 123
    assert info["fingerprint"] == "chrome"
    assert info["online"] is True
    assert info["token"] == "alice-token"
    assert info["link"].endswith("?token=alice-token")
    assert "bob-token" not in frame.decode()
    assert "auth_token" not in info
    assert info["bandwidth"] == {
        "total": {"upload": 10, "download": 20, "total": 30},
        "wl_total": {"upload": 30, "download": 40, "total": 70},
        "monthly": 1234,
        "wl_monthly": 5678,
        "limit": 10,
        "wl_limit": 20,
    }
    assert snapshot.authorized_frame("alice", "b" * 100) is None
    assert snapshot.authorized_frame("missing", "a" * 100) is None
    expired = snapshot.users["alice"].copy()
    expired["auth_token_expires_at"] = int(time.time()) - 1
    assert (
        UpdateSnapshot({"alice": expired}, snapshot.frames).authorized_frame("alice", "a" * 100)
        is None
    )

    await database.set_user("alice", displayname="Updated", uuid="new-id", fingerprint="firefox")
    assert await sub.update_svc.snapshot("alice") is snapshot
    clock[0] = 2
    updated = await sub.update_svc.snapshot("alice")
    assert records.await_count == 2
    changed = updated.authorized_frame("alice", "a" * 100)
    assert changed is not None
    info = json.loads(changed.removeprefix(b"data: "))
    assert info["displayname"] == "Updated"
    assert info["uuid"] == "new-id"
    assert info["fingerprint"] == "firefox"
    assert info["bandwidth"]["total"]["upload"] == 0
    assert info["bandwidth"]["wl_total"]["download"] == 0

    await database.set_auth_token("alice", None)
    clock[0] = 4
    assert (await sub.update_svc.snapshot("alice")).authorized_frame("alice", "a" * 100) is None


async def test_updates_reject_invalid_traffic(database: Database) -> None:
    panel = FakePanel(
        get_payload={
            "success": True,
            "obj": [{"email": "alice", "uuid": "id", "traffic": {"up": "invalid"}}],
        }
    )
    sub = await make_subscription(database, panels=[panel])
    sub.update_svc.add_viewer("alice")
    with pytest.raises(PanelUnavailableError, match="invalid client list"):
        await sub.update_svc.snapshot("alice")


async def test_updates_and_online_status_share_ttl_and_invalidation(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await database.create_user(
        username="alice", uuid="alice-id", token="token", fingerprint="chrome", displayname="Alice"
    )
    panel = FakePanel(post_payload={"success": True, "obj": ["alice", "foreign"]})
    sub = await make_subscription(database, panels=[panel])
    sub.update_svc.add_viewer("alice")
    clock = 100.0
    monkeypatch.setattr("core.services.panel.monotonic", lambda: clock)
    first, status = await asyncio.gather(
        sub.update_svc.snapshot("alice"), sub.panel_svc.get_online_status()
    )
    assert first.online == frozenset({"alice"})
    assert status.users == ["alice"]
    assert len(panel.posts) == 1
    empty_online: dict[str, JsonValue] = {"success": True, "obj": []}
    panel._post_payload = empty_online
    clock += 4
    assert (await sub.panel_svc.get_online_status()).users == ["alice"]
    assert len(panel.posts) == 1
    clock += 1
    assert (await sub.panel_svc.get_online_status()).users == []
    assert len(panel.posts) == 2
    await sub.panel_svc.invalidate_clients(sub.panels[0])
    await sub.panel_svc.unfiltered_onlines()
    assert len(panel.posts) == 3


async def test_updates_ignore_mismatched_traffic_and_unknown_online_users(
    database: Database,
) -> None:
    await database.create_user(
        username="alice", uuid="alice-id", token="token", fingerprint="chrome", displayname="Alice"
    )
    await database.set_auth_token("alice", "a" * 100)
    client = make_panel_client("alice", [1], 10, 20, uuid="alice-id")
    assert client.traffic is not None
    panel = FakePanel(
        clients=[replace(client, traffic=replace(client.traffic, email="other"))],
        post_payload={"success": True, "obj": ["unknown"]},
    )
    sub = await make_subscription(database, panels=[panel])
    sub.update_svc.add_viewer("alice")
    snapshot = await sub.update_svc.snapshot("alice")
    frame = snapshot.authorized_frame("alice", "a" * 100)
    assert frame is not None
    info = json.loads(frame.removeprefix(b"data: "))
    assert info["online"] is False
    assert not snapshot.online
    assert info["bandwidth"]["total"] == {"upload": 0, "download": 0, "total": 0}
    assert snapshot.authorized_frame("unknown", "a" * 100) is None


async def test_updates_sum_matching_duplicate_panel_rows(database: Database) -> None:
    await database.create_user(
        username="alice", uuid="alice-id", token="token", fingerprint="chrome", displayname="Alice"
    )
    panel = FakePanel(
        clients=[
            make_panel_client("alice", [1], 10, 20, uuid="alice-id"),
            make_panel_client("alice", [2], 30, 40, uuid="alice-id"),
            make_panel_client("alice", [3], 50, 60, uuid="old-id"),
        ]
    )
    sub = await make_subscription(database, panels=[panel])
    sub.update_svc.add_viewer("alice")
    snapshot = await sub.update_svc.snapshot("alice")
    assert snapshot.totals["alice"] == [40, 60, 0, 0]


async def test_updates_failure_and_cancellation_are_shared(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = await make_subscription(database)
    sub.update_svc.add_viewer("alice")
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail() -> None:
        entered.set()
        await release.wait()
        raise RuntimeError("unavailable")

    load = AsyncMock(side_effect=fail)
    monkeypatch.setattr(sub.update_svc, "_collect", load)
    first = asyncio.create_task(sub.update_svc.snapshot("alice"))
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    for _ in range(10):
        with pytest.raises(RuntimeError, match="unavailable"):
            await sub.update_svc.snapshot("alice")
    assert load.await_count == 1
