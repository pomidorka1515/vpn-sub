from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Never
from unittest.mock import AsyncMock, patch

import pytest
from helpers import create_alice, make_subscription

from errors import CommittedAuditError, NotFoundError

if TYPE_CHECKING:
    from core import Subscription
    from db import Database


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[Mapping[str, object]] = []

    async def append(self, record: Mapping[str, object]) -> None:
        self.records.append(record)


@pytest.mark.asyncio
async def test_audit_is_silent_without_config(
    database: Database,
) -> None:
    subscription = await make_subscription(database)
    (await create_alice(database))
    (await subscription.audit_svc.audit(name="user_add", info={"username": "alice"}))
    (await subscription.business_svc.update_params("alice", displayname="Alice 2"))


@pytest.mark.asyncio
async def test_audit_appends_when_configured(database: Database) -> None:
    audit = RecordingAudit()
    subscription = await make_subscription(
        database,
        audit_cfg=audit,
    )
    (await create_alice(database))
    (await subscription.business_svc.update_params("alice", displayname="Alice 2"))
    assert audit.records
    last = audit.records[-1]
    assert last["action"] == "user_update_params"
    info = last["info"]
    assert isinstance(info, Mapping)
    assert info["username"] == "alice"
    assert info["displayname"] == "Alice 2"


@pytest.mark.asyncio
async def test_bonus_audit_failure_reports_committed_result(
    database: Database, subscription: Subscription,
) -> None:
    await create_alice(database, bw_limit_gb=1)
    await subscription.code_svc.add_code("bonus", "bonus", gb=3, uses=2)
    with patch.object(subscription.audit_svc, "audit", new=AsyncMock(side_effect=OSError)):
        with pytest.raises(CommittedAuditError, match="Do not retry") as caught:
            await subscription.code_svc.apply_bonus_code(username="alice", code="bonus")
    assert isinstance(caught.value.result, dict)
    assert caught.value.result["limit"] == 4
    assert caught.value.result["uses"] == 1
    assert (await subscription.user_svc.user("alice"))["bw_limit_gb"] == 4
    assert (await subscription.code_svc.get_code("bonus")).uses == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "reset", "register"])
async def test_identity_audit_failure_preserves_committed_credentials(
    database: Database, subscription: Subscription, operation: str,
) -> None:
    if operation == "reset":
        await create_alice(database)
    if operation == "register":
        await subscription.code_svc.add_code("invite", "register")

    async def mutate() -> None:
        if operation == "create":
            await subscription.business_svc.add_new_user("alice", "Alice")
        elif operation == "reset":
            await subscription.business_svc.reset_user("alice")
        else:
            await subscription.business_code_svc.register_with_code(
                code="invite", username="alice", displayname="Alice",
                ext_username="alice-login", ext_password="secret",
            )

    with patch.object(subscription.audit_svc, "audit", new=AsyncMock(side_effect=OSError)):
        with pytest.raises(CommittedAuditError) as caught:
            await mutate()
    user = await subscription.user_svc.user("alice")
    result = caught.value.result
    assert isinstance(result, dict)
    assert result["uuid"] == user["uuid"]
    assert result["token"] == user["token"]
    assert not await database.registration_pending("alice")
    assert await database.get_metadata("user_create_pending:alice") is None
    assert await database.get_metadata("user_reset_pending:alice") is None


@pytest.mark.asyncio
async def test_validation_failure_is_not_reported_as_committed(subscription: Subscription) -> None:
    with patch.object(subscription.audit_svc, "audit", new=AsyncMock(side_effect=OSError)) as audit:
        with pytest.raises(NotFoundError):
            await subscription.code_svc.apply_bonus_code(username="missing", code="missing")
    audit.assert_not_called()


@pytest.mark.asyncio
async def test_api_reports_committed_audit_failure_as_success() -> None:
    from api.application import create_api_application

    app = create_api_application()

    @app.get("/committed")
    async def committed() -> Never:
        raise CommittedAuditError(result={"token": "committed-token"})

    response = await app.test_client().get("/committed")
    assert response.status_code == 202
    body = await response.get_json()
    assert body["success"] is True
    assert body["obj"] == {
        "committed": True, "audit_pending": True, "token": "committed-token",
    }


@pytest.mark.asyncio
async def test_code_management_audit_failures_are_committed(subscription: Subscription) -> None:
    with patch.object(subscription.audit_svc, "audit", new=AsyncMock(side_effect=OSError)):
        with pytest.raises(CommittedAuditError):
            await subscription.code_svc.add_code("bonus", "bonus")
        assert (await subscription.code_svc.get_code("bonus")).uses == 1
        with pytest.raises(CommittedAuditError):
            await subscription.code_svc.delete_code("bonus")
        with pytest.raises(NotFoundError):
            await subscription.code_svc.get_code("bonus")


@pytest.mark.asyncio
async def test_update_params_drops_cache_before_reporting_audit_failure(
    database: Database, subscription: Subscription,
) -> None:
    await create_alice(database)
    with (
        patch.object(subscription.audit_svc, "audit", new=AsyncMock(side_effect=OSError)),
        patch.object(subscription.business_svc, "_drop_cache", new=AsyncMock()) as drop_cache,
    ):
        with pytest.raises(CommittedAuditError):
            await subscription.business_svc.update_params("alice", displayname="Updated")
        drop_cache.assert_awaited_once()
    assert (await subscription.user_svc.user("alice"))["displayname"] == "Updated"


@pytest.mark.asyncio
async def test_reset_reports_pending_uuid_audit_even_if_reset_audit_succeeds(
    database: Database, subscription: Subscription,
) -> None:
    await create_alice(database)
    with patch.object(
        subscription.audit_svc, "audit", new=AsyncMock(side_effect=[OSError(), None]),
    ) as audit:
        with pytest.raises(CommittedAuditError) as caught:
            await subscription.business_svc.reset_user("alice")
    assert audit.await_count == 2
    assert isinstance(caught.value.result, dict)
    user = await subscription.user_svc.user("alice")
    assert caught.value.result["uuid"] == user["uuid"]
    assert caught.value.result["token"] == user["token"]
    assert await database.get_metadata("user_reset_pending:alice") is None
