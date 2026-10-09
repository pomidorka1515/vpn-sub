from __future__ import annotations

from flask import Flask

from api.common import BaseApi, Route
from bwatch import BWatch
from config import AppConfig, Config
from core import Subscription
from loggers import Logger

from .account import AccountRoutes
from .session import SessionRoutes
from .static import StaticRoutes

__all__ = ["WebApi"]


class WebApi(
    AccountRoutes,
    SessionRoutes,
    StaticRoutes,
    BaseApi,
):
    """Public, user-facing API."""

    ROUTES: list[Route] = [
        *StaticRoutes.ROUTES,
        *SessionRoutes.ROUTES,
        *AccountRoutes.ROUTES,
    ]

    def __init__(self,
                 app: Flask,
                 cfg: Config[AppConfig],
                 sub: Subscription,
                 bw: BWatch):
        self.log = Logger(type(self).__name__)
        conf = cfg.view()
        uri = '/' + '/'.join(p for p in conf['uri'].split('/') if p)
        super().__init__(app, cfg, sub, bw, uri)
        self.prefix = self.uri.rstrip('/')
