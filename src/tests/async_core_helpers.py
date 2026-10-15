from __future__ import annotations

from typing import TYPE_CHECKING, Unpack
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from helpers import FakePanel, config_mock, language_config, subscription_config

from app_runtime.offload import OffloadServices
from app_runtime.tasks import MutationRegistry
from core import Subscription
from core.coordination import MutationCoordinator
from core.services.audit import AuditService
from session.client import XUiSession
from session.transport import PanelResponse

if TYPE_CHECKING:
    from pathlib import Path

    from db.database import Database
    from session.transport import RequestKwargs


class PanelTransport:
    def __init__(self, panel: FakePanel) -> None:
        self.panel = panel

    async def request(
        self,
        method: str,
        url: str,
        **kwargs: Unpack[RequestKwargs],
    ) -> PanelResponse:
        route = urlsplit(url).path.lstrip("/")
        response = self.panel.get(route) if method == "GET" else self.panel.post(route, **kwargs)
        return PanelResponse(response.status_code, response.content, dict(response.headers))


async def make_async_subscription(
    db: Database,
    directory: Path,
    panels: list[FakePanel] | None = None,
    whitelist: FakePanel | None = None,
) -> Subscription:
    clients = [
        XUiSession(
            name=panel.name,
            address="127.0.0.1",
            port=8080,
            uri="",
            api_token="test",
            transport=PanelTransport(panel),
            stamp_dir=str(directory),
            inbounds_list=panel.inbounds_list,
            mode="whitelist" if panel.mode == "whitelist" else "blacklist",
        )
        for panel in ([FakePanel(name="fixture-panel")] if panels is None else panels)
    ]
    wl = (
        XUiSession(
            name=whitelist.name,
            address="127.0.0.1",
            port=8080,
            uri="",
            api_token="test",
            transport=PanelTransport(whitelist),
            stamp_dir=str(directory),
        )
        if whitelist is not None
        else None
    )
    for client in [*clients, *([wl] if wl else [])]:
        await client.start()
    return Subscription(
        cfg=config_mock(subscription_config(fingerprints=["chrome", "firefox"])),
        lang_cfg=config_mock(language_config()),
        db=db,
        panels=clients,
        whitelist_panel=wl,
        offloads=OffloadServices(),
        coordinator=MutationCoordinator(MutationRegistry()),
        audit_svc=AuditService(None),
        password_hasher=PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1),
    )


async def close_async_subscription(sub: Subscription) -> None:
    await sub.close()
    for panel in sub.panels:
        await panel.close()
    await sub.res.offloads.close()
