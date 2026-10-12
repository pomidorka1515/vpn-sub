from __future__ import annotations

from typing import TYPE_CHECKING

from custom_types import ClientPayload

if TYPE_CHECKING:
    from custom_types import PanelClient
    from errors import PanelRejectedError


def client_payload_from(client: PanelClient) -> ClientPayload:
    """Echo the complete row on full-replacement panel updates."""
    return ClientPayload(
        email=client.email,
        id=client.uuid,
        flow=client.flow,
        limitIp=client.limitIp,
        totalGB=client.totalGB,
        expiryTime=client.expiryTime,
        enable=client.enable,
        tgId=client.tgId,
        subId=client.subId,
        comment=client.comment,
        reset=client.reset,
    )


def already_exists(exc: PanelRejectedError) -> bool:
    message = exc.message.lower()
    return "duplicate" in message or "already exist" in message
