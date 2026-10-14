from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from async_core_helpers import close_async_subscription, make_async_subscription
from helpers import USER_UUID, FakePanel, make_inbound, make_panel_client

from db.database import Database
from errors import (
    ConflictError,
    NotFoundError,
    PanelRejectedError,
    PanelUnavailableError,
    ValidationError,
)
from session.transport import PanelResponse

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping
    from pathlib import Path

    from config import JsonValue
    from session.client import XUiSession

NEW_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.mark.asyncio
async def test_online_health_cache_rename_and_retry(
    async_db: Database,
    tmp_path: Path,
) -> None:
    good = FakePanel(
        name="good",
        post_payload={"success": True, "msg": "", "obj": ["alice", "foreign"]},
    )
    invalid = FakePanel(
        name="invalid",
        post_payload={"success": True, "msg": "", "obj": ["alice", 1]},
    )
    dead = FakePanel(name="dead")
    sub = await make_async_subscription(async_db, tmp_path, [good, invalid, dead])
    sub.panels[2].dead = True
    try:
        await create_alice(async_db)
        await async_db.update_user("alice", ext_username="old")
        status = await sub.panel_svc.get_online_status()
        assert status.users == ["alice"]
        assert status.panel_health == {"good": "ok", "invalid": "invalid", "dead": "unavailable"}
        await async_db.update_user("alice", ext_username="new")
        assert (await sub.panel_svc.get_online_status(new=True)).users == {"alice": "new"}
        assert len(good.posts) == 1
        for panel in sub.panels:
            panel.dead = True
        with pytest.raises(PanelUnavailableError):
            await sub.panel_svc.get_online_status(fresh=True)
        sub.panels[0].dead = False
        assert (await sub.panel_svc.get_online_status(fresh=True)).users == ["alice"]
    finally:
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_ambiguous_accepted_add_is_compensated(
    async_db: Database,
    tmp_path: Path,
) -> None:
    panel = FakePanel(inbounds=[make_inbound(1)])
    sub = await make_async_subscription(async_db, tmp_path, [panel])
    original = sub.business_svc._post

    async def lost_response(
        panel: XUiSession,
        route: str,
        body: Mapping[str, JsonValue],
        what: str,
    ) -> None:
        sub.business_svc._post = original  # type: ignore[method-assign]
        await original(panel, route, body, what)
        raise PanelUnavailableError("response lost after accepted add")

    try:
        await sub.code_svc.add_code("invite", "register")
        sub.business_svc._post = lost_response  # type: ignore[method-assign]
        with pytest.raises(PanelUnavailableError):
            await sub.business_code_svc.register_with_code(
                code="invite",
                username="alice",
                displayname="Alice",
                ext_username="alice",
                ext_password="secret",
            )
        assert panel.clients == []
        assert not await async_db.user_exists("alice")
        assert await async_db.get_code("invite") is not None
        with pytest.raises(PanelUnavailableError):
            sub.panel_svc.response_data(
                sub.panels[0],
                PanelResponse.from_json({"success": False}, 404),
                "client query",
            )
    finally:
        await close_async_subscription(sub)


@pytest_asyncio.fixture
async def async_db(tmp_path: Path) -> AsyncGenerator[Database]:
    db = Database(path=tmp_path / "state.sqlite3", start_backup=False)
    await db.start()
    try:
        yield db
    finally:
        await db.close()


async def create_alice(db: Database) -> None:
    await db.create_user(
        username="alice",
        uuid=USER_UUID,
        token="a" * 40,
        fingerprint="chrome",
        displayname="Alice",
        bw_limit_gb=10,
        wl_limit_gb=5,
    )


@pytest.mark.asyncio
async def test_user_code_credentials_and_telegram_workflow(
    async_db: Database,
    tmp_path: Path,
) -> None:
    sub = await make_async_subscription(async_db, tmp_path)
    try:
        with pytest.raises(NotFoundError):
            await sub.business_svc.get_info("missing")
        created = await sub.business_svc.add_new_user(
            "alice",
            "Alice",
            ext_username="alice-login",
            ext_password="secret",
            limit=10,
        )
        with pytest.raises(ConflictError):
            await sub.business_svc.add_new_user("alice", "Alice")
        with pytest.raises(ValidationError):
            await sub.business_svc.update_params("alice", fingerprint="invalid")
        assert await sub.password_svc.validate_credentials("alice-login", "wrong") is None
        assert await sub.password_svc.validate_credentials("alice-login", "secret") == "alice"
        await sub.telegram_svc.set_telegram_user(123, "alice")
        await sub.telegram_svc.set_telegram_language(123, "en")
        assert await sub.telegram_svc.get_username_telegram(123) == "alice"
        assert await sub.telegram_svc.get_username_telegram("alice", reverse=True) == 123
        assert await sub.telegram_svc.get_telegram_language(123) == "en"
        await sub.code_svc.add_code("bonus", "bonus", gb=3, wl_gb=2)
        bonus = await sub.telegram_svc.bonus_code(123, "bonus")
        assert (bonus.limit, bonus.wl_limit) == (13, 7)
        await sub.business_svc.update_params("alice", displayname="Alice 2", fingerprint="firefox")
        info = await sub.business_svc.get_info("alice")
        assert info.link == f"https://example.test/sub?token={created.token}"
        assert (info.displayname, info.fingerprint) == ("Alice 2", "firefox")
        await sub.user_svc.set_auth_token("alice", "x" * 100)
        reset = await sub.business_svc.reset_user("alice")
        assert reset.uuid != created.uuid
        assert reset.token != created.token
        assert await sub.user_svc.auth_token_to_user("x" * 100) is None
        await sub.business_svc.delete_user("alice", perma=True)
        assert not await sub.user_svc.isuser("alice")
        assert sub.res.coordinator.lock_count == 0
    finally:
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_bandwidth_history_leaderboard_and_legacy_password(
    async_db: Database,
    tmp_path: Path,
) -> None:
    regular = FakePanel(
        name="regular",
        clients=[
            make_panel_client("alice", [1, 2], up=10, down=20),
            make_panel_client("foreign", [1], up=999, down=999),
        ],
    )
    whitelist = FakePanel(name="whitelist", clients=[make_panel_client("alice", [3], up=5, down=7)])
    sub = await make_async_subscription(async_db, tmp_path, [regular], whitelist)
    try:
        await create_alice(async_db)
        await async_db.set_user(
            "alice",
            ext_username="alice",
            ext_password=sub.password_svc.legacy_hash("secret"),
        )
        assert await sub.password_svc.validate_credentials("alice", "secret") == "alice"
        stored = await async_db.ext_password("alice")
        assert stored is not None
        assert stored.startswith("$argon2id$")
        assert await sub.password_svc.validate_credentials("alice", "wrong") is None
        await async_db.set_user("alice", ext_password="$argon2id$corrupt")
        assert await sub.password_svc.validate_credentials("alice", "secret") is None
        assert (await sub.bandwidth_svc.bandwidth("alice")).total == 30
        assert (await sub.bandwidth_svc.bandwidth("alice", whitelist=True)).total == 12
        totals = await sub.bandwidth_svc.all_traffic(work="background")
        assert list(totals) == ["alice"]
        assert totals["alice"].total == 30
        await async_db.increment_usage("alice", regular=100, whitelist=50)
        assert await sub.leaderboard_svc.leaderboard("total") == {"alice": 30}
        assert await sub.leaderboard_svc.leaderboard("monthly", use_displaynames=True) == {
            "Alice": 100,
        }
        assert await sub.leaderboard_svc.leaderboard("wl_monthly") == {"alice": 50}
        assert await sub.bandwidth_svc.get_bw_history("alice") == []
        assert await sub.bandwidth_svc.get_snapshots() == []
    finally:
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_uuid_partial_failure_retains_manual_repair_marker(
    async_db: Database,
    tmp_path: Path,
) -> None:
    accepted = FakePanel(name="accepted", clients=[make_panel_client("alice", [1])])
    rejected = FakePanel(
        name="rejected",
        clients=[make_panel_client("alice", [1])],
        post_payload={"success": False, "msg": "rejected", "obj": None},
    )
    sub = await make_async_subscription(async_db, tmp_path, [accepted, rejected])
    try:
        await create_alice(async_db)
        with pytest.raises(PanelRejectedError):
            await sub.business_svc.update_uuid("alice", NEW_UUID)
        assert accepted.clients[0].uuid == NEW_UUID
        user = await async_db.get_user("alice")
        assert user is not None
        assert user["uuid"] == USER_UUID
        failures = await sub.business_code_svc.get_rollback_failures()
        assert "rejected" in failures["uuid"]["alice"]["reason"]
        with pytest.raises(ConflictError):
            await sub.business_svc.reconcile_user("alice")
    finally:
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_failed_disable_and_delete_are_recovered(
    async_db: Database,
    tmp_path: Path,
) -> None:
    panel = FakePanel(
        clients=[make_panel_client("alice", [1])],
        inbounds=[make_inbound(1)],
        post_payload={"success": False, "msg": "rejected", "obj": None},
    )
    sub = await make_async_subscription(async_db, tmp_path, [panel])
    try:
        await create_alice(async_db)
        with pytest.raises(PanelRejectedError):
            await sub.business_svc.update_user("alice", enable=False)
        user = await async_db.get_user("alice")
        assert user is not None
        assert not user["enabled"]
        assert panel.clients[0].enable
        assert await async_db.get_metadata("user_sync_pending:alice") == "enabled"
        panel._post_payload = None
        await sub.business_svc.recover_pending()
        assert not bool(panel.clients[0].enable)
        assert await async_db.get_metadata("user_sync_pending:alice") is None
        rejected_payload: dict[str, JsonValue] = {"success": False, "msg": "rejected", "obj": None}
        panel._post_payload = rejected_payload
        with pytest.raises(PanelRejectedError):
            await sub.business_svc.delete_user("alice", perma=True)
        assert await async_db.get_metadata("user_delete_pending:alice") is not None
        panel._post_payload = None
        await sub.business_svc.recover_pending()
        assert panel.clients == []
        assert not await async_db.user_exists("alice")
        assert await async_db.get_metadata("user_delete_pending:alice") is None
    finally:
        await close_async_subscription(sub)


@pytest.mark.asyncio
@pytest.mark.parametrize("user_count", [0, 1, 9])
async def test_reconcile_uses_bounded_workers_and_user_ownership(
    async_db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    user_count: int,
) -> None:
    sub = await make_async_subscription(async_db, tmp_path)
    users = [f"user{index}" for index in range(user_count)]
    started: list[str] = []
    active = 0
    peak = 0
    saturated = asyncio.Event()
    release = asyncio.Event()

    async def list_users() -> list[str]:
        return users

    async def add_users(
        username: str,
        _called_internally: bool = False,
        *,
        work: str = "foreground",
    ) -> None:
        nonlocal active, peak
        assert _called_internally
        assert work == "background"
        task = asyncio.current_task()
        assert task is not None
        assert sub.res.coordinator._owners[task] == frozenset([username])
        started.append(username)
        active += 1
        peak = max(peak, active)
        if active == min(4, user_count):
            saturated.set()
        try:
            await release.wait()
        finally:
            active -= 1

    async def sync_enabled(username: str, *, work: str = "foreground") -> None:
        assert work == "background"
        if int(username.removeprefix("user")) % 2 == 0:
            raise PanelRejectedError("test reconciliation failure")

    monkeypatch.setattr(sub.user_svc, "list_users", list_users)
    monkeypatch.setattr(sub.business_svc, "add_users", add_users)
    monkeypatch.setattr(sub.business_svc, "_sync_enabled", sync_enabled)
    reconcile = asyncio.create_task(sub.business_svc.reconcile())
    try:
        if users:
            async with asyncio.timeout(5):
                await saturated.wait()
            assert len(started) == min(4, user_count)
            workers = [
                task
                for task in asyncio.all_tasks()
                if task.get_name().startswith("core:reconcile-worker:")
            ]
            assert len(workers) == min(4, user_count)
        release.set()
        async with asyncio.timeout(5):
            assert await reconcile == users[::2]
        assert sorted(started) == users
        assert peak == min(4, user_count)
        assert sub.res.coordinator.lock_count == 0
    finally:
        release.set()
        await reconcile
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_panel_payloads_reconcile_and_whitelist_separation(
    async_db: Database,
    tmp_path: Path,
) -> None:
    regular = FakePanel(
        name="regular",
        inbounds=[make_inbound(1), make_inbound(2), make_inbound(3, protocol="trojan")],
    )
    whitelist = FakePanel(name="whitelist", inbounds=[make_inbound(4)])
    sub = await make_async_subscription(async_db, tmp_path, [regular], whitelist)
    try:
        await create_alice(async_db)
        await sub.business_svc.add_users("alice")
        body = regular.posts[0][1]["json"]
        assert isinstance(body, dict)
        assert body["inboundIds"] == [1, 2]
        assert (
            regular.clients[0].totalGB,
            regular.clients[0].expiryTime,
            regular.clients[0].limitIp,
        ) == (0, 0, 0)
        regular._inbounds.append(make_inbound(5))
        await sub.panel_svc.invalidate_inbounds(sub.panels[0])
        assert await sub.business_svc.reconcile() == []
        assert regular.clients[0].inboundIds == [1, 2, 5]
        await sub.business_svc.update_user("alice", enable=False)
        assert not regular.clients[0].enable
        assert whitelist.clients[0].enable
        regular.clients[0] = replace(regular.clients[0], comment="keep", tgId="777", subId="sub-id")
        await sub.business_svc.update_uuid("alice", NEW_UUID)
        updated = regular.clients[0]
        assert (updated.uuid, updated.comment, updated.tgId, updated.subId) == (
            NEW_UUID,
            "keep",
            "777",
            "sub-id",
        )
    finally:
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_registration_one_shot_code_race(async_db: Database, tmp_path: Path) -> None:
    sub = await make_async_subscription(async_db, tmp_path)
    try:
        await sub.code_svc.add_code("invite", "register")
        results = await asyncio.gather(
            *(
                sub.business_code_svc.register_with_code(
                    code="invite",
                    username=name,
                    displayname=name,
                    ext_username=name,
                    ext_password="secret",
                )
                for name in ("alice", "bob")
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(result, Exception) for result in results) == 1
        assert sum(isinstance(result, NotFoundError) for result in results) == 1
        assert len(await async_db.list_users()) == 1
        assert await async_db.pending_registrations() == []
    finally:
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_registration_partial_writes_cleaned_before_code_refund(
    async_db: Database,
    tmp_path: Path,
) -> None:
    accepted = FakePanel(name="accepted", inbounds=[make_inbound(1)])
    rejected = FakePanel(
        name="rejected",
        inbounds=[make_inbound(1)],
        post_payload={"success": False, "msg": "rejected", "obj": None},
    )
    sub = await make_async_subscription(async_db, tmp_path, [accepted, rejected])
    try:
        await sub.code_svc.add_code("invite", "register")
        with pytest.raises(PanelRejectedError):
            await sub.business_code_svc.register_with_code(
                code="invite",
                username="alice",
                displayname="Alice",
                ext_username="alice",
                ext_password="secret",
            )
        assert accepted.clients == []
        assert not await async_db.user_exists("alice")
        code = await async_db.get_code("invite")
        assert code is not None
        assert code["uses"] == 1
        assert await async_db.pending_registrations() == []
    finally:
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_failed_panel_cleanup_retains_identity_and_recovery_refunds_once(
    async_db: Database,
    tmp_path: Path,
) -> None:
    accepted = FakePanel(name="accepted", inbounds=[make_inbound(1)])
    rejected = FakePanel(
        name="rejected",
        inbounds=[make_inbound(1)],
        post_payload={"success": False, "msg": "rejected", "obj": None},
    )
    sub = await make_async_subscription(async_db, tmp_path, [accepted, rejected])
    original = sub.business_svc.rollback_panel_clients

    async def fail_cleanup(
        username: str,
        *,
        expected_uuid: str | None = None,
        work: str = "foreground",
    ) -> None:
        raise PanelUnavailableError("cleanup unavailable")

    try:
        await sub.code_svc.add_code("invite", "register")
        sub.business_svc.rollback_panel_clients = fail_cleanup  # type: ignore[method-assign]
        with pytest.raises(PanelRejectedError):
            await sub.business_code_svc.register_with_code(
                code="invite",
                username="alice",
                displayname="Alice",
                ext_username="alice",
                ext_password="secret",
            )
        assert await async_db.user_exists("alice")
        assert await async_db.pending_registrations() == ["alice"]
        assert await async_db.get_code("invite") is None
        assert await async_db.get_metadata("registration_rollback_failed:alice") is not None
        with pytest.raises(ConflictError):
            await sub.business_svc.reset_user("alice")
        sub.business_svc.rollback_panel_clients = original  # type: ignore[method-assign]
        await sub.business_code_svc.recover_rollback_failures()
        await sub.business_code_svc.recover_rollback_failures()
        assert accepted.clients == []
        assert not await async_db.user_exists("alice")
        code = await async_db.get_code("invite")
        assert code is not None
        assert code["uses"] == 1
    finally:
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_atomic_created_user_marker_and_rollback_repository(async_db: Database) -> None:
    await async_db.create_user(
        username="alice",
        uuid=USER_UUID,
        token="a" * 40,
        fingerprint="chrome",
        displayname="Alice",
        pending_sync=True,
    )
    assert await async_db.get_metadata("user_create_pending:alice") == USER_UUID
    await async_db.set_metadata("user_sync_pending:alice", "reconcile")
    await async_db.rollback_created_user("alice", USER_UUID)
    await async_db.rollback_created_user("alice", USER_UUID)
    assert not await async_db.user_exists("alice")
    assert await async_db.get_metadata("user_create_pending:alice") is None
    assert await async_db.get_metadata("user_sync_pending:alice") is None


@pytest.mark.asyncio
async def test_reset_finalization_and_startup_recovery(
    async_db: Database,
    tmp_path: Path,
) -> None:
    from errors import DatabaseError

    sub = await make_async_subscription(async_db, tmp_path)
    try:
        await create_alice(async_db)
        await async_db.set_auth_token("alice", "x" * 100)
        await async_db.set_metadata(
            "user_reset_pending:alice",
            '{"uuid":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","token":"new-token"}',
        )
        with pytest.raises(DatabaseError, match="reset UUID"):
            await async_db.finalize_reset("alice", NEW_UUID, "new-token")
        assert await async_db.get_metadata("user_reset_pending:alice") is not None
        await async_db.update_user("alice", uuid=NEW_UUID)
        await sub.start()
        user = await async_db.get_user("alice")
        assert user is not None
        assert user["token"] == "new-token"
        assert user["auth_token"] is None
        assert await async_db.get_metadata("user_reset_pending:alice") is None
    finally:
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_pending_creation_panel_cleanup_recovers(
    async_db: Database,
    tmp_path: Path,
) -> None:
    panel = FakePanel(clients=[make_panel_client("alice", [1])])
    sub = await make_async_subscription(async_db, tmp_path, [panel])
    try:
        await async_db.create_user(
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            pending_sync=True,
        )
        await sub.start()
        assert panel.clients == []
        assert not await async_db.user_exists("alice")
        assert await async_db.get_metadata("user_create_pending:alice") is None
    finally:
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_pending_registration_without_marker_is_recovered(
    async_db: Database,
    tmp_path: Path,
) -> None:
    panel = FakePanel(clients=[make_panel_client("alice", [1])])
    sub = await make_async_subscription(async_db, tmp_path, [panel])
    try:
        await async_db.add_code("invite", "register")
        await async_db.register_with_code(
            code="invite",
            username="alice",
            uuid=USER_UUID,
            token="a" * 40,
            fingerprint="chrome",
            displayname="Alice",
            ext_username="alice",
            ext_password_hash="hash",
        )
        await sub.start()
        assert panel.clients == []
        assert await async_db.pending_registrations() == []
        assert await async_db.get_code("invite") is not None
    finally:
        await close_async_subscription(sub)
