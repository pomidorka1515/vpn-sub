from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Literal, TypedDict

from requests import Session

from config import JsonValue, ProfileConfig
from config.documents import (
    BotConfig,
    PanelConfig,
    ProfileOutbound,
    ProfileTemplate,
    PublicBotConfig,
    RedisConfig,
)
from custom_types import Inbound, PanelClient, UserInfoBandwidth
from session.transport import XUiPanelTransport


class UserInfoOverrides(TypedDict, total=False):
    _: str
    token: str
    link: str
    displayname: str
    uuid: str
    fingerprint: str
    enabled: bool
    wl_enabled: bool
    time: int
    online: bool
    bandwidth: UserInfoBandwidth


class CodeOverrides(TypedDict, total=False):
    code: str
    action: str
    perma: bool
    uses: int
    days: int
    gb: int
    wl_gb: int


class UserRecordOverrides(TypedDict, total=False):
    username: str
    uuid: str
    token: str
    auth_token: str | None
    fingerprint: str
    displayname: str
    enabled: int
    enabled_time: int
    enabled_wl: int
    expires_at: int
    bw_limit_gb: int
    bw_used: int
    wl_limit_gb: int
    wl_used: int
    ext_username: str | None
    ext_password_hash: str | None
    created_at: int


class CreateUserOverrides(TypedDict, total=False):
    username: str
    uuid: str
    token: str
    fingerprint: str
    displayname: str
    expires_at: int
    bw_limit_gb: int
    wl_limit_gb: int
    ext_username: str | None
    ext_password_hash: str | None
    enabled: bool
    enabled_time: bool
    enabled_wl: bool


AppOverrides = TypedDict("AppOverrides", {
    "uri": str, "api_token": str, "provider_id": str, "salt": str,
    "domain": str, "ping_check_url": str, "sub_name": str,
    "api_admin_ui_auth": list[str], "fingerprints": list[str],
    "bypass_packages": list[str], "panel_alert_cooldown": int,
    "nodes": dict[str, str], "json_template": ProfileTemplate,
    "3xui": dict[str, PanelConfig], "profiles": dict[str, ProfileConfig],
    "redis": RedisConfig, "bot": BotConfig, "publicbot": PublicBotConfig,
    "api_uri": str, "fallback_domain": str, "$schema": str,
    "funny_strings": list[str],
}, total=False)


class ProfileOverrides(TypedDict, total=False):
    flag: str
    name: list[str]
    json: ProfileOutbound
    description: list[str]
    whitelist: bool
    xhttpExtra: dict[str, JsonValue]
    masterLink: str
    node: str
    shortProfileDescription: list[str]


class FakePanelOptions(TypedDict, total=False):
    inbounds: list[Inbound]
    clients: list[PanelClient]
    name: str
    local: bool
    dead: bool
    mode: str
    inbounds_list: tuple[int, ...]
    post_payload: dict[str, JsonValue]
    post_status: int
    post_error: BaseException
    post_queue: list[dict[str, JsonValue] | BaseException]
    get_payload: dict[str, JsonValue]
    get_status: int
    get_error: BaseException
    status_payload: dict[str, JsonValue]
    status_status: int
    status_error: BaseException


class SessionExtras(TypedDict, total=False):
    https: bool
    nginx_auth: tuple[str, str] | None
    inbounds_list: tuple[int, ...]
    mode: Literal["whitelist", "blacklist"]
    inject_headers: Mapping[str, str | bytes] | None
    health_check_interval: int
    verbose: bool
    transport: XUiPanelTransport | None
    session: Session | None
    clock: Callable[[], float]
    stamp_path: str | None
    stamp_dir: str | None
    client_stamp_path: str | None


class SessionOptions(SessionExtras, total=False):
    name: str
    address: str
    port: int | str
    uri: str
    api_token: str
