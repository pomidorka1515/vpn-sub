from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from .constants import JsonDict

# Contracts from config.schema.json and src/discord/config.schema.json;
# lang.jsonc and src/discord/lang.jsonc have no schemas.


class BotConfig(TypedDict):
    token: str
    whitelist: list[int]


class PublicBotConfig(TypedDict):
    token: str


class RedisConfig(TypedDict):
    url: str
    socket_timeout: NotRequired[float]


class PanelConfig(TypedDict):
    name: str
    address: str
    port: str | int
    uri: str
    token: str
    https: bool
    whitelist: bool
    inbounds_list: list[int]
    mode: Literal["whitelist", "blacklist"]
    nginx_auth: NotRequired[list[str]]
    inject_headers: NotRequired[JsonDict]


class ProfileConfig(TypedDict):
    flag: str
    name: list[str]
    json: JsonDict
    description: list[str]
    whitelist: bool
    xhttpExtra: JsonDict
    masterLink: str
    node: str
    shortProfileDescription: list[str]


AppConfig = TypedDict("AppConfig", {
    "uri": str,
    "api_token": str,
    "provider_id": str,
    "salt": str,
    "domain": str,
    "ping_check_url": str,
    "sub_name": str,
    "api_admin_ui_auth": list[str],
    "fingerprints": list[str],
    "bypass_packages": list[str],
    "panel_alert_cooldown": int,
    "nodes": dict[str, str],
    "json_template": JsonDict,
    "3xui": dict[str, PanelConfig],
    "profiles": dict[str, ProfileConfig],
    "redis": RedisConfig,
    "bot": NotRequired[BotConfig],
    "publicbot": NotRequired[PublicBotConfig],
    "api_uri": NotRequired[str],
    "fallback_domain": NotRequired[str],
    "$schema": NotRequired[str],
    "funny_strings": NotRequired[list[str]],
})


class DiscordBotConfig(TypedDict):
    token: str


class DiscordPrivateConfig(TypedDict):
    whitelist: list[int]
    api_token: str


DiscordConfig = TypedDict("DiscordConfig", {
    "public": DiscordBotConfig,
    "private": DiscordPrivateConfig,
    "$schema": NotRequired[str],
})


class LangConfig(TypedDict):
    description: dict[str, dict[str, str]]
    chart: dict[str, dict[str, str]]
    publicbot: dict[str, dict[str, str]]
    web: JsonDict


class DiscordLangConfig(TypedDict):
    public: dict[str, dict[str, str]]
    chart: dict[str, dict[str, str]]
