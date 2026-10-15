from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from async_core_helpers import close_async_subscription, make_async_subscription

from errors import NoPanelsConfiguredError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from db import Database


@pytest.mark.asyncio
async def test_panel_workflows_have_no_side_effects(database: Database, tmp_path: Path) -> None:
    sub = await make_async_subscription(database, tmp_path, [])
    try:
        await database.create_user(
            username="alice", uuid="01234567-89ab-cdef-0123-456789abcdef",
            token="a" * 40, fingerprint="chrome", displayname="Alice",
        )
        before = await database.get_user("alice")
        metadata = await database.list_metadata("")
        operations: list[Callable[[], Awaitable[object]]] = [
            lambda: sub.business_svc.add_new_user("bob", "Bob"),
            lambda: sub.business_code_svc.register_with_code(
                code="invite", username="bob", displayname="Bob",
                ext_username="bob", ext_password="password",
            ),
            lambda: sub.business_svc.delete_user("alice", perma=True),
            lambda: sub.account_svc.delete("alice", "password"),
            lambda: sub.business_svc.update_user("alice", enable=False),
            lambda: sub.business_svc.update_uuid("alice", "11234567-89ab-cdef-0123-456789abcdef"),
            lambda: sub.business_svc.reset_user("alice"),
            lambda: sub.business_svc.add_users("alice"),
            lambda: sub.business_svc.apply_quota("missing"),
            lambda: sub.business_svc.get_info("missing"),
            lambda: sub.business_svc.reconcile_user("alice"),
            sub.business_svc.reconcile,
            sub.refresh_svc.refresh,
            lambda: sub.bandwidth_svc.bandwidth("alice"),
            sub.bandwidth_svc.all_traffic,
            sub.panel_svc.get_online_status,
        ]
        for operation in operations:
            with (
                patch.object(sub.res.coordinator, "run", side_effect=AssertionError("mutation admitted")),
                patch.object(sub.user_svc, "user", side_effect=AssertionError("user read")),
                pytest.raises(NoPanelsConfiguredError) as error,
            ):
                await operation()
            assert error.value.status == 503
            assert await database.get_user("alice") == before
            assert await database.get_user("bob") is None
            assert await database.list_metadata("") == metadata
        await sub.business_svc.update_params("alice", displayname="Renamed")
        assert (await sub.user_svc.user("alice"))["displayname"] == "Renamed"
    finally:
        await close_async_subscription(sub)


@pytest.mark.asyncio
async def test_panel_free_start_preserves_pending_work(database: Database, tmp_path: Path) -> None:
    sub = await make_async_subscription(database, tmp_path, [])
    markers = {
        "user_create_pending:alice": "uuid",
        "user_delete_pending:bob": '{"uuid":"uuid","perma":true}',
        "user_reset_pending:carol": '{"uuid":"uuid","token":"token"}',
        "user_sync_pending:dave": "enabled",
        "registration_rollback_failed:erin": "123",
    }
    try:
        before = await database.list_metadata("")
        for key, value in markers.items():
            await database.set_metadata(key, value)
        await sub.start()
        assert await database.list_metadata("") == before | markers
    finally:
        await close_async_subscription(sub)
