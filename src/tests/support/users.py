from __future__ import annotations

from typing import TYPE_CHECKING, Unpack

if TYPE_CHECKING:
    from db import Database
    from tests.support.contracts import CreateUserOverrides

USER_UUID = "01234567-89ab-cdef-0123-456789abcdef"
TOKEN_A = "a" * 40


async def create_alice(database: Database, **kwargs: Unpack[CreateUserOverrides]) -> None:
    payload: CreateUserOverrides = {
        "username": "alice",
        "uuid": USER_UUID,
        "token": TOKEN_A,
        "fingerprint": "chrome",
        "displayname": "Alice",
    }
    payload.update(kwargs)
    await database.create_user(
        username=payload["username"],
        uuid=payload["uuid"],
        token=payload["token"],
        fingerprint=payload["fingerprint"],
        displayname=payload["displayname"],
        expires_at=payload.get("expires_at", 0),
        bw_limit_gb=payload.get("bw_limit_gb", 0),
        wl_limit_gb=payload.get("wl_limit_gb", 0),
        ext_username=payload.get("ext_username"),
        ext_password_hash=payload.get("ext_password_hash"),
        enabled=payload.get("enabled", True),
        enabled_time=payload.get("enabled_time", True),
        enabled_wl=payload.get("enabled_wl", True),
    )
