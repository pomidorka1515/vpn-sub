from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quart import Quart, Response

    from api.common import ResponseType, SnapshotStatus
    from config import AppConfig
    from config.async_store import AsyncConfig
    from core import AsyncSubscription
    from loggers import Logger


class AsyncWebApiMixin:
    """Attributes every web route group reads from the composed WebApi."""

    app: Quart
    cfg: AsyncConfig[AppConfig]
    sub: AsyncSubscription
    bw: SnapshotStatus
    uri: str
    log: Logger
    prefix: str

    async def validate_auth_token(self, auth_token: str | None = None) -> str | None:
        raise NotImplementedError

    async def validate_credentials(self, username: str, password: str) -> str | bool:
        raise NotImplementedError

    def _clear_auth_cookies(self, response: Response) -> None:
        raise NotImplementedError

    async def _static(self, name: str, mimetype: str, body: bytes | None = None) -> ResponseType:
        raise NotImplementedError

    async def _page(self, name: str) -> Response:
        raise NotImplementedError
