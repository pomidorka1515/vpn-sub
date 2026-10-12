from __future__ import annotations

from typing import TYPE_CHECKING, Unpack
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from helpers import config_mock, language_config, subscription_config

from app_runtime.offload import OffloadServices
from app_runtime.tasks import MutationRegistry
from core import AsyncSubscription
from core.coordination import MutationCoordinator
from core.services.async_audit import AsyncAuditService
from session.async_client import AsyncXUiSession
from session.async_transport import PanelResponse

if TYPE_CHECKING:
    from pathlib import Path

    from helpers import FakePanel

    from db.async_database import AsyncDatabase
    from session.async_transport import AsyncRequestKwargs


class PanelTransport:
    def __init__(self, panel: FakePanel) -> None:
        self.panel = panel

    async def request(self, method: str, url: str, **kwargs: Unpack[AsyncRequestKwargs]) -> PanelResponse:
        route = urlsplit(url).path.lstrip("/")
        response = self.panel.get(route) if method == "GET" else self.panel.post(route, **kwargs)
        return PanelResponse(response.status_code, response.content, dict(response.headers))


async def make_async_subscription(db: AsyncDatabase, directory: Path, panels: list[FakePanel] | None = None, whitelist: FakePanel | None = None) -> AsyncSubscription:
    clients = [AsyncXUiSession(name=panel.name, address="127.0.0.1", port=8080, uri="", api_token="test", transport=PanelTransport(panel), stamp_dir=str(directory), inbounds_list=panel.inbounds_list, mode="whitelist" if panel.mode == "whitelist" else "blacklist") for panel in panels or []]
    wl = AsyncXUiSession(name=whitelist.name, address="127.0.0.1", port=8080, uri="", api_token="test", transport=PanelTransport(whitelist), stamp_dir=str(directory)) if whitelist is not None else None
    for client in [*clients, *([wl] if wl else [])]:
        await client.start()
    return AsyncSubscription(cfg=config_mock(subscription_config(fingerprints=["chrome", "firefox"])), lang_cfg=config_mock(language_config()), db=db, panels=clients, whitelist_panel=wl, offloads=OffloadServices(), coordinator=MutationCoordinator(MutationRegistry()), audit_svc=AsyncAuditService(None), password_hasher=PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1))


async def close_async_subscription(sub: AsyncSubscription) -> None:
    await sub.close()
    for panel in sub.panels:
        await panel.close()
    await sub.res.offloads.close()
