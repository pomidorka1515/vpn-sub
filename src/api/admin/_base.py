from __future__ import annotations

from flask import Flask

from bwatch import BWatch
from config import AppConfig, Config, LinesConfig
from core import Subscription
from loggers import Logger


class AdminApiMixin:
    """Attributes every admin route group reads from the composed Api."""

    app: Flask
    cfg: Config[AppConfig]
    sub: Subscription
    bw: BWatch
    uri: str
    log: Logger
    token: str
    audit_cfg: LinesConfig
