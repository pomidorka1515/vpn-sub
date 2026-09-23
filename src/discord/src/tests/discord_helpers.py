from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from config import ConfigLike
from public.bot import PublicBot
from sessions import SessionStore
from webapi import WebApiClient

DISCORD_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = DISCORD_ROOT / "config.schema.json"
EXAMPLE_PATH = DISCORD_ROOT / "docs" / "EXAMPLE.config.json"
LANG_PATH = DISCORD_ROOT / "lang.jsonc"
LANG = cast(ConfigLike, json.loads(LANG_PATH.read_text(encoding="utf-8")))


class FakeHeaders(dict[str, str]):
    def getall(self, key: str, default: list[str] | None = None) -> list[str]:
        if key in self:
            return [self[key]]
        for item, value in self.items():
            if item.lower() == key.lower():
                return [value]
        return default or []


class FakeResponse:
    def __init__(
        self,
        status: int,
        headers: Mapping[str, str],
        payload: object | None = None,
        body: bytes = b"",
        content_type: str = "application/json",
    ) -> None:
        self.status = status
        self.headers = FakeHeaders(dict(headers))
        self._payload = payload
        self._body = body
        self.content_type = content_type

    async def json(self, content_type: str | None = None) -> object:
        del content_type
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> bool:
        del args
        return False


class FakeSession:
    closed = False

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.calls: list[dict[str, object]] = []

    def request(
        self,
        method: str,
        url: str,
        json: Mapping[str, object] | None = None,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> FakeResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "json": dict(json) if json is not None else None,
                "params": dict(params) if params is not None else None,
                "headers": dict(headers) if headers is not None else None,
            }
        )
        result = self.handler(method, url, json, params, headers)
        if not isinstance(result, FakeResponse):
            raise TypeError("handler must return FakeResponse")
        return result


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeResponseAPI:
    def __init__(self) -> None:
        self.done = False
        self.deferred = False
        self.messages: list[dict[str, object]] = []
        self.modals: list[object] = []

    def is_done(self) -> bool:
        return self.done

    async def send_message(self, **kwargs: object) -> None:
        self.done = True
        self.messages.append(kwargs)

    async def send_modal(self, modal: object) -> None:
        self.done = True
        self.modals.append(modal)

    async def defer(self, **kwargs: object) -> None:
        del kwargs
        self.done = True
        self.deferred = True


class FakeFollowup:
    def __init__(self, response: FakeResponseAPI) -> None:
        self.response = response

    async def send(self, **kwargs: object) -> None:
        self.response.messages.append(kwargs)


class FakeInteraction:
    def __init__(
        self,
        user_id: int = 7,
        *,
        data: dict[str, object] | None = None,
        guild_id: int | None = None,
    ) -> None:
        self.user = FakeUser(user_id)
        self.data = data or {}
        self.guild_id = guild_id
        self.channel = None
        self.type = None
        self.response = FakeResponseAPI()
        self.followup = FakeFollowup(self.response)


def json_ok(obj: object = None, msg: str = "ok", status: int = 200) -> FakeResponse:
    return FakeResponse(status, {}, {"success": True, "msg": msg, "obj": obj})


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def stats_obj() -> dict[str, object]:
    return {
        "displayname": "alice",
        "enabled": True,
        "wl_enabled": False,
        "online": True,
        "time": 0,
        "fingerprint": "chrome",
        "link": "https://example.test/sub/alice",
        "bandwidth": {
            "total": {"upload": 10, "download": 20},
            "wl_total": {"upload": 1, "download": 2},
            "monthly": 3,
            "wl_monthly": 4,
            "limit": 100,
            "wl_limit": 50,
        },
    }


def modal_data(custom_id: str, fields: Mapping[str, str]) -> dict[str, object]:
    return {
        "custom_id": custom_id,
        "components": [
            {
                "components": [
                    {"custom_id": key, "value": value} for key, value in fields.items()
                ]
            }
        ],
    }


def make_web_client(handler: Any) -> tuple[WebApiClient, FakeSession]:
    session = FakeSession(handler)
    client = WebApiClient(
        "http://127.0.0.1:5550",
        "sub",
        session=session,  # type: ignore[arg-type]
    )
    return client, session


def make_public_bot(
    tmp_path: Path,
    handler: Any,
    *,
    user_id: int = 7,
) -> tuple[PublicBot, SessionStore, FakeSession]:
    store = SessionStore(tmp_path / "discord-sessions.json")
    store.set_lang(user_id, "en")
    store.set_token(user_id, "tok")
    session = FakeSession(handler)
    bot = PublicBot(
        cast(ConfigLike, {"public": {"token": "discord-token"}}),
        LANG,
        WebApiClient("http://127.0.0.1:5550", "sub", session=session),  # type: ignore[arg-type]
        store,
    )
    return bot, store, session
