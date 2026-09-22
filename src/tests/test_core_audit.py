from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from config import LinesConfigLike
from db import Database
from helpers import create_alice, make_subscription


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[Mapping[str, object]] = []

    def append(self, record: Mapping[str, Any]) -> None:
        self.records.append(record)


def test_audit_is_silent_without_config(
    database: Database,
) -> None:
    subscription = make_subscription(database)
    create_alice(database)
    subscription.audit_svc.audit(name="user_add", info={"username": "alice"})
    subscription.business_svc.update_params("alice", displayname="Alice 2")


def test_audit_appends_when_configured(database: Database) -> None:
    audit = RecordingAudit()
    subscription = make_subscription(
        database, audit_cfg=cast(LinesConfigLike, audit),
    )
    create_alice(database)
    subscription.business_svc.update_params("alice", displayname="Alice 2")
    assert audit.records
    last = audit.records[-1]
    assert last["action"] == "user_update_params"
    info = last["info"]
    assert isinstance(info, Mapping)
    assert info["username"] == "alice"
    assert info["displayname"] == "Alice 2"
