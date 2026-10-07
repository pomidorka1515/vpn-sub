from __future__ import annotations

from flask import Flask, Response

from api.common import ResponseType
from bwatch import BWatch
from config import ConfigLike
from core import Subscription
from loggers import Logger


class WebApiMixin:
    """Attributes every web route group reads from the composed WebApi."""

    app: Flask
    cfg: ConfigLike
    sub: Subscription
    bw: BWatch
    uri: str
    log: Logger
    prefix: str

    def validate_auth_token(self, auth_token: str | None = None) -> str | None:
        raise NotImplementedError

    def validate_credentials(self, username: str, password: str) -> str | bool:
        raise NotImplementedError

    def _clear_auth_cookies(self, response: Response) -> None:
        raise NotImplementedError

    def _static(self, name: str, mimetype: str, body: bytes | None = None) -> ResponseType:
        raise NotImplementedError

    def _page(self, name: str) -> Response:
        raise NotImplementedError
