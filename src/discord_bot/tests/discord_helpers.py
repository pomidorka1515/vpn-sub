from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
from app_runtime.offload import BoundedOffload
from app_runtime.rate_limit import AsyncRateLimiter
from config import AppConfig, Config, DiscordLangConfig
from core import Subscription
from custom_types import BandwidthSnapshot, UserInfo, UserInfoBandwidth, UserInfoBandwidthTotal
from discord_bot.admin import AdminBot
from discord_bot.host import SharedDiscordClient
from discord_bot.public import PublicBot
from discord_bot.sessions import SessionRecord

if TYPE_CHECKING:
    from collections.abc import Coroutine, Mapping

LANG_PATH = Path(__file__).resolve().parents[2] / "discord/lang.jsonc"
LANG = Config[DiscordLangConfig](path=LANG_PATH, read_only=True, read_only_jsonc=True)


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeResponseAPI:
    def __init__(self) -> None:
        self.done = False
        self.deferred = False
        self.messages: list[dict[str, object]] = []
        self.modals: list[object] = []
        self.defers: list[dict[str, object]] = []

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, **kwargs: object) -> None:
        self.done = True
        self.messages.append(kwargs)

    async def send_modal(self, modal: object) -> None:
        self.done = True
        self.modals.append(modal)

    async def defer(self, **kwargs: object) -> None:
        self.defers.append(kwargs)
        self.done = True
        self.deferred = True


class FakeFollowup:
    def __init__(self, response: FakeResponseAPI) -> None:
        self.response = response

    async def send(self, **kwargs: object) -> None:
        self.response.messages.append(kwargs)


class FakeInteraction:
    def __init__(self, user_id: int = 7, *, data: dict[str, object] | None = None, guild_id: int | None = None) -> None:
        self.user = FakeUser(user_id)
        self.data = data or {}
        self.guild_id = guild_id
        self.channel = None
        self.type = None
        self.response = FakeResponseAPI()
        self.followup = FakeFollowup(self.response)


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def stats_obj() -> UserInfo:
    return UserInfo(
        _="alice", token="a" * 40, uuid="uuid", displayname="alice", enabled=True,
        wl_enabled=False, online=True, time=0, fingerprint="chrome",
        link="https://example.test/sub/alice",
        bandwidth=UserInfoBandwidth(
            total=UserInfoBandwidthTotal(upload=10, download=20, total=30),
            wl_total=UserInfoBandwidthTotal(upload=1, download=2, total=3),
            monthly=3, wl_monthly=4, limit=100, wl_limit=50,
        ),
    )


def modal_data(custom_id: str, fields: Mapping[str, str]) -> dict[str, object]:
    return {"custom_id": custom_id, "components": [
        {"components": [{"custom_id": key, "value": value} for key, value in fields.items()]},
    ]}


class MemorySessionStore:
    def __init__(self) -> None:
        self.records: dict[str, SessionRecord] = {}

    def get(self, user_id: int | str) -> SessionRecord:
        key = str(user_id)
        return self.records.get(key, SessionRecord(key, "", None))

    def logged_in(self, user_id: int | str) -> bool:
        return self.get(user_id).logged_in

    def token(self, user_id: int | str) -> str | None:
        return self.get(user_id).token or None

    def lang(self, user_id: int | str) -> str | None:
        return self.get(user_id).lang

    def set_lang(self, user_id: int | str, lang: str) -> None:
        if lang in {"en", "ru"}:
            old = self.get(user_id)
            self.records[old.user_id] = SessionRecord(old.user_id, old.token, lang)

    def set_token(self, user_id: int | str, token: str) -> None:
        old = self.get(user_id)
        self.records[old.user_id] = SessionRecord(old.user_id, token, old.lang)

    def clear_token(self, user_id: int | str) -> None:
        self.set_token(user_id, "")


def services() -> MagicMock:
    sub = MagicMock(spec=Subscription)
    sub.user_svc = MagicMock()
    sub.user_svc.auth_token_to_user = AsyncMock(return_value="alice")
    sub.user_svc.external_username_exists = AsyncMock(return_value=False)
    sub.user_svc.list_users = AsyncMock(return_value=["alice", "bob"])
    sub.business_svc = MagicMock()
    sub.business_svc.get_info = AsyncMock(return_value=stats_obj())
    for method in ("reset_user", "delete_user", "update_params", "add_new_user"):
        setattr(sub.business_svc, method, AsyncMock())
    sub.account_svc = MagicMock()
    sub.account_svc.login = AsyncMock(return_value="newtoken")
    for method in ("settings", "logout", "delete"):
        setattr(sub.account_svc, method, AsyncMock())
    sub.business_code_svc = MagicMock()
    sub.business_code_svc.register_with_code = AsyncMock()
    sub.code_svc = MagicMock()
    for method in ("apply_bonus_code", "add_code", "delete_code", "get_code", "list_code"):
        setattr(sub.code_svc, method, AsyncMock())
    sub.bandwidth_svc = MagicMock()
    sub.bandwidth_svc.get_bw_history = AsyncMock(return_value=[BandwidthSnapshot(ts=1, up=10, down=20, wl_up=1, wl_down=2)])
    sub.leaderboard_svc = MagicMock()
    sub.leaderboard_svc.leaderboard = AsyncMock(return_value={"alice": 10})
    sub.refresh_svc = MagicMock()
    sub.refresh_svc.refresh = AsyncMock()
    return sub


def make_bots(*, whitelist: list[int] | None = None, rendering: BoundedOffload | None = None) -> tuple[PublicBot, AdminBot, MemorySessionStore, MagicMock]:
    cfg = MagicMock(spec=Config)
    cfg.view.return_value = cast(AppConfig, {
        "discord": {"token": "discord-token", "whitelist": whitelist if whitelist is not None else [7]},
        "domain": "https://example.test", "uri": "sub", "fingerprints": ["chrome", "firefox"],
        "profiles": {"default": {"name": ["Default", "По умолчанию"], "description": ["VPN", "VPN"]}},
    })
    store = MemorySessionStore()
    store.set_lang(7, "en")
    store.set_token(7, "token")
    host = SharedDiscordClient(intents=discord.Intents.none())
    limiter = MagicMock(spec=AsyncRateLimiter)
    limiter.allow = AsyncMock(return_value=True)
    sub = services()
    pool = rendering or BoundedOffload(capacity=2, name="test-rendering")
    public = PublicBot(cfg, LANG, sub, store, client=host, tree=host.tree, rendering=pool, limiter=limiter)
    admin = AdminBot(cfg, LANG, sub, client=host, tree=host.tree, rendering=pool, limiter=limiter)
    host.attach(public, admin)
    return public, admin, store, sub
