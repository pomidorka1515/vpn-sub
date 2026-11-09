from __future__ import annotations

import tempfile
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Unpack, cast

from argon2 import PasswordHasher

from app_runtime.offload import OffloadServices
from app_runtime.tasks import MutationRegistry
from config.store import AsyncLinesConfig
from core import Subscription
from core.coordination import MutationCoordinator
from core.services.audit import AuditService
from session.client import XUiSession
from tests.support.config import config_mock, language_config, subscription_config
from tests.support.panels import FakePanel, PanelTransport

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from config import Config, LangConfig
    from db.database import Database
    from tests.support.contracts import AppOverrides


_owner: ContextVar[dict[int, Subscription] | None] = ContextVar("subscription_owner", default=None)


@asynccontextmanager
async def subscription_scope() -> AsyncGenerator[None]:
    """Own subscriptions created in this test; close them before its database."""
    owned: dict[int, Subscription] = {}
    token = _owner.set(owned)
    try:
        async with AsyncExitStack() as cleanup:
            try:
                yield
            finally:
                for sub in owned.values():
                    cleanup.push_async_callback(close_subscription, sub)
    finally:
        _owner.reset(token)


async def make_subscription(
    db: Database,
    directory: Path | None = None,
    panels: list[FakePanel] | None = None,
    whitelist_panel: FakePanel | None = None,
    *,
    audit_cfg: object | None = None,
    lang_cfg: Config[LangConfig] | None = None,
    **config_overrides: Unpack[AppOverrides],
) -> Subscription:
    config = subscription_config(**config_overrides)
    if directory is None:
        directory = Path(tempfile.mkdtemp(dir=Path(db.path).parent))
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
            name=whitelist_panel.name,
            address="127.0.0.1",
            port=8080,
            uri="",
            api_token="test",
            transport=PanelTransport(whitelist_panel),
            stamp_dir=str(directory),
        )
        if whitelist_panel is not None
        else None
    )
    offloads = OffloadServices()
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(offloads.close)
        for client in [*clients, *([wl] if wl else [])]:
            cleanup.push_async_callback(client.close)
            await client.start()
        sub = Subscription(
            cfg=config_mock(config),
            lang_cfg=lang_cfg if lang_cfg is not None else config_mock(language_config()),
            db=db,
            panels=clients,
            whitelist_panel=wl,
            offloads=offloads,
            coordinator=MutationCoordinator(MutationRegistry()),
            audit_svc=AuditService(cast(AsyncLinesConfig | None, audit_cfg)),
            password_hasher=PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1),
        )
        cleanup.pop_all()
    if (owned := _owner.get()) is not None:
        owned[id(sub)] = sub
    return sub


async def close_subscription(sub: Subscription) -> None:
    if (owned := _owner.get()) is not None:
        owned.pop(id(sub), None)
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(sub.res.offloads.close)
        for panel in sub.panels:
            cleanup.push_async_callback(panel.close)
        await sub.close()
