from __future__ import annotations

from flask import Flask

from api.common import BaseApi, Route
from bwatch import BWatch
from config import ConfigLike, LinesConfigLike
from core import Subscription
from loggers import Logger

from .code import CodeRoutes
from .config import ConfigRoutes
from .logs import LogsRoutes
from .operations import OperationsRoutes
from .panel import PanelRoutes
from .state import StateRoutes
from .ui import UiRoutes
from .user import UserRoutes

__all__ = ["Api"]


class Api(
    UserRoutes,
    PanelRoutes,
    CodeRoutes,
    StateRoutes,
    LogsRoutes,
    OperationsRoutes,
    ConfigRoutes,
    UiRoutes,
    BaseApi,
):
    """Private admin API."""

    ROUTES: list[Route] = [
        *UserRoutes.ROUTES,
        *PanelRoutes.ROUTES,
        *CodeRoutes.ROUTES,
        *StateRoutes.ROUTES,
        *LogsRoutes.ROUTES,
        *OperationsRoutes.ROUTES,
        *ConfigRoutes.ROUTES,
    ]

    def __init__(self,
                 app: Flask,
                 cfg: ConfigLike,
                 audit_cfg: LinesConfigLike,
                 sub: Subscription,
                 bw: BWatch):
        self.log = Logger(type(self).__name__)
        uri = '/' + '/'.join(p.strip('/ ') for p in (cfg['uri'], cfg['api_uri']) if p and p.strip('/ '))
        self.token = cfg['api_token']
        self.audit_cfg = audit_cfg
        super().__init__(app, cfg, sub, bw, uri)
