from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

import pytest
from helpers import create_alice, make_subscription

if TYPE_CHECKING:
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
    subscription = (await make_subscription(database))
    (await create_alice(database))
    (await subscription.audit_svc.audit(name="user_add", info={"username": "alice"}))
    (await subscription.business_svc.update_params("alice", displayname="Alice 2"))


@pytest.mark.asyncio
async def test_audit_appends_when_configured(database: Database) -> None:
    audit = RecordingAudit()
    subscription = (await make_subscription(
        database,
        audit_cfg=audit,
    ))
    (await create_alice(database))
    (await subscription.business_svc.update_params("alice", displayname="Alice 2"))
    assert audit.records
    last = audit.records[-1]
    assert last["action"] == "user_update_params"
    info = last["info"]
    assert isinstance(info, Mapping)
    assert info["username"] == "alice"
    assert info["displayname"] == "Alice 2"
