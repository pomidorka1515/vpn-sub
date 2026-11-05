from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Unpack
from unittest.mock import MagicMock

from config import Config

if TYPE_CHECKING:
    from config import AppConfig, LangConfig, ProfileConfig
    from tests.support.contracts import AppOverrides, ProfileOverrides


def subscription_config(**overrides: Unpack[AppOverrides]) -> AppConfig:
    config: AppConfig = {
        "uri": "sub",
        "fingerprints": ["chrome"],
        "salt": "test-salt",
        "domain": "https://example.test",
        "funny_strings": ["test"],
        "api_token": "secret",
        "provider_id": "",
        "ping_check_url": "https://example.test/204",
        "sub_name": "VPN",
        "api_admin_ui_auth": ["admin", "password"],
        "bypass_packages": [],
        "panel_alert_cooldown": 3600,
        "nodes": {},
        "json_template": {"remarks": "", "outbounds": []},
        "3xui": {},
        "profiles": {},
        "redis": {"url": ""},
    }
    config.update(overrides)
    return config


def config_mock[Doc](document: Doc) -> Config[Doc]:
    mock = MagicMock(spec=Config)
    mock.view.side_effect = lambda: deepcopy(document)
    return mock


def language_config() -> LangConfig:
    return {"description": {}, "chart": {}, "publicbot": {}, "web": {}}


def profile_config(**overrides: Unpack[ProfileOverrides]) -> ProfileConfig:
    document: ProfileConfig = {
        "flag": "",
        "name": ["", ""],
        "json": {"settings": {"vnext": []}, "streamSettings": {}},
        "description": ["", ""],
        "whitelist": False,
        "xhttpExtra": {},
        "masterLink": "",
        "node": "edge",
        "shortProfileDescription": ["", ""],
    }
    document.update(overrides)
    return document
