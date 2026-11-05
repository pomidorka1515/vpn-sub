from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from session.client import XUiSession
from tests.support.panels import FakePanel
from tests.support.subscriptions import close_subscription, make_subscription, subscription_scope

if TYPE_CHECKING:
    from db import Database

pytestmark = pytest.mark.asyncio


async def test_scope_closes_subscriptions_when_its_body_fails(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close = AsyncMock()
    panels_close = AsyncMock()
    files_close = AsyncMock()

    async def failing_body() -> None:
        async with subscription_scope():
            sub = await make_subscription(database)
            close.side_effect = sub.close
            panels_close.side_effect = sub.panels[0].close
            files_close.side_effect = sub.res.offloads.files.close
            monkeypatch.setattr(sub, "close", close)
            monkeypatch.setattr(sub.panels[0], "close", panels_close)
            monkeypatch.setattr(sub.res.offloads.files, "close", files_close)
            raise RuntimeError("test body failed")

    with pytest.raises(RuntimeError, match="test body failed"):
        await failing_body()

    close.assert_awaited_once()
    panels_close.assert_awaited_once()
    files_close.assert_awaited_once()


async def test_explicit_close_releases_scope_ownership(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with subscription_scope():
        sub = await make_subscription(database)
        close = AsyncMock(wraps=sub.close)
        monkeypatch.setattr(sub, "close", close)
        await close_subscription(sub)

    close.assert_awaited_once()


async def test_scope_owns_subscriptions_created_in_child_tasks(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with subscription_scope():
        sub = await asyncio.create_task(make_subscription(database))
        close = AsyncMock(wraps=sub.close)
        monkeypatch.setattr(sub, "close", close)

    close.assert_awaited_once()


async def test_factory_injects_one_consistent_resource_set(database: Database) -> None:
    audit = object()
    sub = await make_subscription(database, audit_cfg=audit, salt="custom-salt")
    for service in (
        sub.panel_svc,
        sub.password_svc,
        sub.user_svc,
        sub.business_svc,
        sub.code_svc,
        sub.business_code_svc,
        sub.account_svc,
        sub.refresh_svc,
    ):
        assert service.res is sub.res
        assert service.res.legacy_salt == "custom-salt"
    assert sub.code_svc.audit_svc is sub.audit_svc
    assert sub.business_code_svc.audit_svc is sub.audit_svc


async def test_factory_unwinds_clients_if_startup_fails(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = AsyncMock(side_effect=[None, RuntimeError("panel startup failed")])
    close = AsyncMock()
    monkeypatch.setattr(XUiSession, "start", start)
    monkeypatch.setattr(XUiSession, "close", close)
    with pytest.raises(RuntimeError, match="panel startup failed"):
        await make_subscription(database, panels=[FakePanel(name="one"), FakePanel(name="two")])
    assert close.await_count == 2
