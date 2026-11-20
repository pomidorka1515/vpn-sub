from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from db.database import Database
from errors import (
    ConflictError,
    NotFoundError,
    PanelRejectedError,
    PanelUnavailableError,
)
from session.transport import PanelResponse
from tests.support.panels import FakePanel, make_inbound, make_panel_client
from tests.support.subscriptions import close_subscription, make_subscription
from tests.support.users import USER_UUID

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Coroutine, Mapping, Sequence
    from pathlib import Path

    from config import JsonValue
    from session.client import XUiSession

NEW_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.mark.asyncio
async def test_subscription_start_runs_recovery_once(
    async_db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sub = await make_subscription(async_db, tmp_path)
    calls: list[str] = []

    async def recover_codes() -> None:
        calls.append("codes")
        await asyncio.sleep(0)

    async def recover_users() -> None:
        calls.append("users")

    monkeypatch.setattr(sub.business_code_svc, "recover_rollback_failures", recover_codes)
    monkeypatch.setattr(sub.business_svc, "recover_pending", recover_users)
    try:
        await asyncio.gather(sub.start(), sub.start())
        await sub.start()
        assert calls == ["codes", "users"]
    finally:
        await close_subscription(sub)


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [None, USER_UUID])
async def test_recovery_rechecks_create_marker_after_waiting_for_ownership(
    async_db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str | None,
) -> None:
    panel = FakePanel(inbounds=[make_inbound(1)])
    sub = await make_subscription(async_db, tmp_path, [panel])
    creation_ready = asyncio.Event()
    snapshot_taken = asyncio.Event()
    delete_metadata = async_db.delete_metadata
    list_metadata = async_db.list_metadata
    key = "user_create_pending:alice"

    async def finish_creation(key_to_delete: str) -> None:
        if key_to_delete == key:
            creation_ready.set()
            await snapshot_taken.wait()
        await delete_metadata(key_to_delete)
        if key_to_delete == key and replacement is not None:
            await async_db.set_metadata(key, replacement)

    async def snapshot(prefix: str) -> dict[str, str]:
        markers = await list_metadata(prefix)
        if prefix == "user_create_pending:":
            assert markers == {key: NEW_UUID}
            snapshot_taken.set()
        return markers

    monkeypatch.setattr(async_db, "delete_metadata", finish_creation)
    monkeypatch.setattr(async_db, "list_metadata", snapshot)
    try:
        async with asyncio.TaskGroup() as group:
            creation = group.create_task(
                sub.business_svc.add_new_user("alice", "Alice", userid=NEW_UUID),
            )
            await creation_ready.wait()
            group.create_task(sub.business_svc.recover_pending())
        assert creation.result().uuid == NEW_UUID
        user = await async_db.get_user("alice")
        assert user is not None
        assert user["uuid"] == NEW_UUID
        assert len(panel.clients) == 1
        assert panel.clients[0].uuid == NEW_UUID
        assert await async_db.get_metadata(key) == replacement
    finally:
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_registration_recovery_does_not_rollback_confirmed_registration(
    async_db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    panel = FakePanel(inbounds=[make_inbound(1)])
    sub = await make_subscription(async_db, tmp_path, [panel])
    confirmation_ready = asyncio.Event()
    recovery_waiting = asyncio.Event()
    confirm = async_db.confirm_registration_sync
    run = sub.res.coordinator.run

    async def delayed_confirm(username: str) -> None:
        confirmation_ready.set()
        await recovery_waiting.wait()
        await confirm(username)

    async def observe_run[T](
        users: Sequence[str], operation: Callable[[], Coroutine[object, object, T]], *, name: str
    ) -> T:
        if name == "core:_rollback_registered_user":
            recovery_waiting.set()
        return await run(users, operation, name=name)

    monkeypatch.setattr(async_db, "confirm_registration_sync", delayed_confirm)
    monkeypatch.setattr(sub.res.coordinator, "run", observe_run)
    try:
        await sub.code_svc.add_code("invite", "register")
        async with asyncio.TaskGroup() as group:
            registration = group.create_task(
                sub.business_code_svc.register_with_code(
                    code="invite",
                    username="alice",
                    displayname="Alice",
                    ext_username="alice",
                    ext_password="secret",
                ),
            )
            await confirmation_ready.wait()
            group.create_task(sub.business_code_svc.recover_rollback_failures())
        created = registration.result()
        user = await async_db.get_user("alice")
        assert user is not None
        assert user["uuid"] == created.uuid
        assert len(panel.clients) == 1
        assert panel.clients[0].uuid == created.uuid
        assert await async_db.pending_registrations() == []
        assert await async_db.get_code("invite") is None
        assert await async_db.get_metadata("registration_rollback_failed:alice") is None
    finally:
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_registration_recovery_skips_replaced_pending_identity(
    async_db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    panel = FakePanel(inbounds=[make_inbound(1)], clients=[make_panel_client("alice", [1])])
    sub = await make_subscription(async_db, tmp_path, [panel])
    recovery_waiting = asyncio.Event()
    run = sub.res.coordinator.run

    async def observe_run[T](
        users: Sequence[str], operation: Callable[[], Coroutine[object, object, T]], *, name: str
    ) -> T:
        if name == "core:_rollback_registered_user":
            recovery_waiting.set()
        return await run(users, operation, name=name)

    async def register(uuid: str, code: str) -> None:
        await async_db.register_with_code(
            code=code,
            username="alice",
            uuid=uuid,
            token=uuid,
            fingerprint="chrome",
            displayname="Alice",
            ext_username="alice",
            ext_password_hash="hash",
        )

    monkeypatch.setattr(sub.res.coordinator, "run", observe_run)
    try:
        await async_db.add_code("first", "register")
        await async_db.add_code("second", "register")

        async def replace_and_release() -> asyncio.Task[None]:
            await register(NEW_UUID, "first")
            recovery = asyncio.create_task(sub.business_code_svc.recover_rollback_failures())
            await recovery_waiting.wait()
            await async_db.rollback_registration_sync("alice", (NEW_UUID, "first", False))
            await register(USER_UUID, "second")
            return recovery

        recovery = await run(["alice"], replace_and_release, name="test:replace-registration")
        await recovery
        assert await async_db.registration_identity("alice") == (USER_UUID, "second", False)
        assert len(panel.clients) == 1
        assert panel.clients[0].uuid == USER_UUID
        assert await async_db.get_code("second") is None
        assert await async_db.get_metadata("registration_rollback_failed:alice") is None
    finally:
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_registration_recovery_preserves_marker_without_pending_identity(
    async_db: Database,
    tmp_path: Path,
) -> None:
    panel = FakePanel(inbounds=[make_inbound(1)])
    sub = await make_subscription(async_db, tmp_path, [panel])
    try:
        await sub.business_svc.add_new_user("alice", "Alice", userid=NEW_UUID)
        await async_db.set_metadata("registration_rollback_failed:alice", "123")
        await sub.business_code_svc.recover_rollback_failures()
        assert await async_db.user_exists("alice")
        assert len(panel.clients) == 1
        assert panel.clients[0].uuid == NEW_UUID
        assert await async_db.get_metadata("registration_rollback_failed:alice") == "123"
    finally:
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_startup_preserves_standalone_uuid_failure_until_manual_clearance(
    async_db: Database,
    tmp_path: Path,
) -> None:
    panel = FakePanel(clients=[make_panel_client("alice", [1], uuid=NEW_UUID)])
    sub = await make_subscription(async_db, tmp_path, [panel])
    try:
        await create_alice(async_db)
        await async_db.set_metadata("uuid_rollback_failed:alice", "123:interrupted")
        await async_db.set_metadata(
            "user_delete_pending:alice",
            json.dumps({"uuid": USER_UUID, "perma": True}),
        )
        await sub.business_svc.recover_pending()
        assert await async_db.get_metadata("uuid_rollback_failed:alice") == "123:interrupted"
        assert await async_db.get_metadata("user_delete_pending:alice") is not None
        assert panel.posts == []
        with pytest.raises(ConflictError, match="UUID recovery"):
            await sub.business_svc.update_params("alice", displayname="Changed")

        # Operator repairs the panel and clears the marker, not a blind retry.
        panel.clients[0] = replace(panel.clients[0], uuid=USER_UUID)
        await async_db.delete_metadata("user_delete_pending:alice")
        await sub.business_code_svc.clear_rollback_failure("uuid", "alice")
        await sub.business_svc.update_params("alice", displayname="Changed")
        user = await async_db.get_user("alice")
        assert user is not None
        assert user["displayname"] == "Changed"
    finally:
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_ambiguous_accepted_add_is_compensated(
    async_db: Database,
    tmp_path: Path,
) -> None:
    panel = FakePanel(inbounds=[make_inbound(1)])
    sub = await make_subscription(async_db, tmp_path, [panel])
    original = sub.business_svc._post

    async def lost_response(
        panel: XUiSession,
        route: str,
        body: Mapping[str, JsonValue],
        what: str,
    ) -> None:
        sub.business_svc._post = original  # type: ignore[method-assign]
        await original(panel, route, body, what)
        raise PanelUnavailableError("response lost after accepted add") from None

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
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_registration_one_shot_code_race(async_db: Database, tmp_path: Path) -> None:
    sub = await make_subscription(async_db, tmp_path)
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
        await close_subscription(sub)


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
    sub = await make_subscription(async_db, tmp_path, [accepted, rejected])
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
        await close_subscription(sub)


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
    sub = await make_subscription(async_db, tmp_path, [accepted, rejected])
    original = sub.business_svc.rollback_panel_clients

    async def fail_cleanup(
        username: str,
        *,
        expected_uuid: str | None = None,
        work: str = "foreground",
    ) -> None:
        raise PanelUnavailableError("cleanup unavailable") from None

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
        await close_subscription(sub)


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
async def test_pending_creation_panel_cleanup_recovers(
    async_db: Database,
    tmp_path: Path,
) -> None:
    panel = FakePanel(clients=[make_panel_client("alice", [1])])
    sub = await make_subscription(async_db, tmp_path, [panel])
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
        await close_subscription(sub)


@pytest.mark.asyncio
async def test_pending_registration_without_marker_is_recovered(
    async_db: Database,
    tmp_path: Path,
) -> None:
    panel = FakePanel(clients=[make_panel_client("alice", [1])])
    sub = await make_subscription(async_db, tmp_path, [panel])
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
        await close_subscription(sub)


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
