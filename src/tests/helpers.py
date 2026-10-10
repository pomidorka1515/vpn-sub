from __future__ import annotations

import json
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Unpack, cast
from unittest.mock import MagicMock
from urllib.parse import unquote

from flask import Flask
from requests import Response

from bots import AdminBot, PublicBot
from bwatch import BWatch
from config import AppConfig, Config, JsonValue, LangConfig, LinesConfig, ProfileConfig
from core import Subscription
from custom_types import ClientTraffic, Inbound, PanelClient
from session import XUiSession

if TYPE_CHECKING:
    from typing_contracts import AppOverrides, CreateUserOverrides, ProfileOverrides

    from db import Database

USER_UUID = "01234567-89ab-cdef-0123-456789abcdef"
TOKEN_A = "a" * 40

type _PostHandler = Callable[[FakePanel, str, dict[str, object]], Response]


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


def make_subscription(
    database: Database,
    *,
    panels: list[object] | None = None,
    whitelist_panel: object | None = None,
    app: Flask | None = None,
    audit_cfg: LinesConfig | None = None,
    lang_cfg: Config[LangConfig] | None = None,
    **config_overrides: Unpack[AppOverrides],
) -> Subscription:
    return Subscription(
        cfg=config_mock(subscription_config(**config_overrides)),
        lang_cfg=config_mock(language_config()) if lang_cfg is None else lang_cfg,
        db=database,
        app=app or Flask(__name__),
        panels=cast(list[XUiSession], panels or []),
        whitelist_panel=cast(XUiSession | None, whitelist_panel),
        audit_cfg=audit_cfg,
    )


def create_alice(database: Database, **kwargs: Unpack[CreateUserOverrides]) -> None:
    payload: CreateUserOverrides = {
        "username": "alice",
        "uuid": USER_UUID,
        "token": TOKEN_A,
        "fingerprint": "chrome",
        "displayname": "Alice",
    }
    payload.update(kwargs)
    database.create_user(
        username=payload["username"],
        uuid=payload["uuid"],
        token=payload["token"],
        fingerprint=payload["fingerprint"],
        displayname=payload["displayname"],
        expires_at=payload.get("expires_at", 0),
        bw_limit_gb=payload.get("bw_limit_gb", 0),
        wl_limit_gb=payload.get("wl_limit_gb", 0),
        ext_username=payload.get("ext_username"),
        ext_password_hash=payload.get("ext_password_hash"),
        enabled=payload.get("enabled", True),
        enabled_time=payload.get("enabled_time", True),
        enabled_wl=payload.get("enabled_wl", True),
    )


def make_watch(
    database: Database,
    subscription: Subscription,
    *,
    bot: object | None = None,
    admin_bot: object | None = None,
) -> BWatch:
    return BWatch(
        cfg=config_mock(subscription_config()),
        db=database,
        sub=subscription,
        bot=cast(PublicBot | None, bot),
        admin_bot=cast(AdminBot | None, admin_bot),
    )


def json_http(data: JsonValue, status_code: int = 200) -> Response:
    response = Response()
    response.status_code = status_code
    response._content = json.dumps(data).encode("utf-8")
    response.headers["Content-Type"] = "application/json"
    return response


def make_client(uuid: str, up: int, down: int, inbound_id: int = 1) -> ClientTraffic:
    return ClientTraffic(
        id=inbound_id,
        inboundId=inbound_id,
        enable=True,
        email="alice",
        uuid=uuid,
        subId="",
        up=up,
        down=down,
        expiryTime=0,
        total=0,
        reset=0,
        lastOnline=0,
    )


def make_panel_client(
    email: str,
    inbound_ids: list[int],
    up: int = 0,
    down: int = 0,
    uuid: str = USER_UUID,
) -> PanelClient:
    return PanelClient(
        email=email,
        uuid=uuid,
        subId="",
        enable=True,
        flow="",
        limitIp=0,
        totalGB=0,
        expiryTime=0,
        tgId="",
        comment="",
        reset=0,
        inboundIds=inbound_ids,
        traffic=ClientTraffic(
            id=0,
            inboundId=0,
            enable=True,
            email=email,
            uuid=uuid,
            subId="",
            up=up,
            down=down,
            expiryTime=0,
            total=0,
            reset=0,
        ),
    )


def make_inbound(
    inbound_id: int,
    clients: list[ClientTraffic] | None = None,
    protocol: str = "vless",
) -> Inbound:
    return Inbound(
        id=inbound_id,
        up=0,
        down=0,
        total=0,
        remark="test",
        enable=True,
        expiryTime=0,
        trafficReset="",
        lastTrafficResetTime=0,
        clientStats=clients or [],
        listen="",
        port=443,
        protocol=protocol,
        settings={},
        streamSettings={"network": "tcp"},
        tag="",
        sniffing={},
    )


class FakePanel:
    def __init__(
        self,
        *,
        inbounds: list[Inbound] | None = None,
        clients: list[PanelClient] | None = None,
        name: str = "panel",
        local: bool = True,
        dead: bool = False,
        mode: str = "blacklist",
        inbounds_list: tuple[int, ...] = (),
        post_payload: dict[str, JsonValue] | None = None,
        post_status: int = 200,
        post_error: BaseException | None = None,
        post_queue: list[dict[str, JsonValue] | BaseException] | None = None,
        get_payload: dict[str, JsonValue] | None = None,
        get_status: int = 200,
        get_error: BaseException | None = None,
        status_payload: dict[str, JsonValue] | None = None,
        status_status: int = 200,
        status_error: BaseException | None = None,
    ) -> None:
        self.local = local
        self.dead = dead
        self.inbounds_list = inbounds_list
        self.mode = mode
        self._cache: list[Inbound] | None = None
        self._cache_set_at: float = 0.0
        self.cache_time: float = 0
        self._cache_generation: int = 0
        self._seen_generation: int = 0
        self._clients_cache: dict[str, PanelClient] | None = None
        self._clients_set_at: float = 0.0
        self._clients_generation: int = 0
        self._clients_seen: int = 0
        self.name = name
        self._inbounds = inbounds or []
        self.clients: list[PanelClient] = clients or []
        self._post_payload = post_payload
        self._post_status = post_status
        self._post_error = post_error
        self._post_queue = list(post_queue or [])
        self._get_payload = get_payload
        self._get_status = get_status
        self._get_error = get_error
        self._status_payload = status_payload
        self._status_status = status_status
        self._status_error = status_error
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict[str, object]]] = []

    @property
    def cache(self) -> list[Inbound] | None:
        return self._cache

    @cache.setter
    def cache(self, value: list[Inbound] | None) -> None:
        self._cache = value
        self._cache_set_at = time.monotonic() if value is not None else 0.0
        self.cache_time = self._cache_set_at
        if value is not None:
            self._seen_generation = self._cache_generation

    @property
    def cache_current(self) -> bool:
        return self._cache is not None and self._seen_generation == self._cache_generation

    @property
    def cache_age(self) -> float:
        if self._cache is None:
            return float("inf")
        return time.monotonic() - self._cache_set_at

    def fresh_cache(self, ttl: float) -> list[Inbound] | None:
        if not self.cache_current or self.cache_age >= ttl:
            return None
        return self._cache

    def clear_cache(self) -> None:
        self._cache_generation += 1
        self.cache = None

    @property
    def clients_cache(self) -> dict[str, PanelClient] | None:
        return self._clients_cache

    @clients_cache.setter
    def clients_cache(self, value: dict[str, PanelClient] | None) -> None:
        self._clients_cache = value
        self._clients_set_at = time.monotonic() if value is not None else 0.0
        if value is not None:
            self._clients_seen = self._clients_generation

    def fresh_clients(self, ttl: float) -> dict[str, PanelClient] | None:
        if self._clients_cache is None or self._clients_seen != self._clients_generation:
            return None
        if time.monotonic() - self._clients_set_at >= ttl:
            return None
        return self._clients_cache

    def clear_clients(self) -> None:
        self._clients_generation += 1
        self.clients_cache = None

    def close(self) -> None:
        pass

    def get(self, url: str) -> Response:
        self.gets.append(url)
        if "server/status" in url:
            if self._status_error is not None:
                raise self._status_error
            payload: dict[str, JsonValue] = (
                self._status_payload
                if self._status_payload is not None
                else {"success": True, "msg": "", "obj": {}}
            )
            return json_http(payload, self._status_status)
        if self._get_error is not None:
            raise self._get_error
        if self._get_payload is not None:
            return json_http(self._get_payload, self._get_status)
        if "clients/list" in url:
            return json_http(
                {
                    "success": True,
                    "msg": "",
                    "obj": [asdict(client) for client in self.clients],
                },
                self._get_status,
            )
        if "clients/get/" in url:
            email = unquote(url.rsplit("/", 1)[-1])
            found = next((c for c in self.clients if c.email == email), None)
            if found is None:
                return json_http(
                    {"success": False, "msg": "Obtain (record not found)", "obj": None}, 200
                )
            client_row = asdict(found)
            inbound_ids = client_row.pop("inboundIds")
            client_row.pop("traffic", None)
            return json_http(
                {
                    "success": True,
                    "msg": "",
                    "obj": {"client": client_row, "inboundIds": inbound_ids},
                },
                self._get_status,
            )
        if "clients/traffic/" in url:
            email = unquote(url.rsplit("/", 1)[-1])
            found = next((c for c in self.clients if c.email == email), None)
            if found is None or found.traffic is None:
                return json_http({"success": True, "msg": "", "obj": None}, 200)
            return json_http(
                {"success": True, "msg": "", "obj": asdict(found.traffic)},
                self._get_status,
            )
        return json_http(
            {
                "success": True,
                "msg": "",
                "obj": [asdict(inbound) for inbound in self._inbounds],
            },
            self._get_status,
        )

    def post(self, url: str, **kwargs: object) -> Response:
        self.posts.append((url, dict(kwargs)))
        if self._post_error is not None:
            raise self._post_error
        if self._post_queue:
            item = self._post_queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            queued = dict(item)
            raw_status = queued.pop("status_code", self._post_status)
            assert isinstance(raw_status, (int, str))
            status = int(raw_status)
            return json_http(queued, status)
        if self._post_payload is not None:
            return json_http(self._post_payload, self._post_status)
        raw_body: object = kwargs.get("json")
        body = dict(cast(dict[str, object], raw_body)) if isinstance(raw_body, dict) else {}
        return self._route_post(url, body)

    def _find(self, email: str) -> PanelClient | None:
        return next((c for c in self.clients if c.email == email), None)

    def _post_add(self, url: str, body: dict[str, object]) -> Response:
        del url
        raw: object = body.get("client")
        if not isinstance(raw, dict):
            return json_http({"success": False, "msg": "missing client", "obj": None}, 200)
        client = cast(dict[str, object], raw)
        email = str(client.get("email", ""))
        if self._find(email) is not None:
            return json_http({"success": False, "msg": "duplicate email", "obj": None}, 200)
        inbound_ids = _integer_list(body.get("inboundIds", []))
        uuid_value = str(client.get("id", ""))
        sub_id = str(client.get("subId", ""))
        self.clients.append(
            PanelClient(
                email=email,
                uuid=uuid_value,
                subId=sub_id,
                enable=bool(client.get("enable", True)),
                flow=str(client.get("flow", "")),
                limitIp=_integer(client.get("limitIp", 0)),
                totalGB=_integer(client.get("totalGB", 0)),
                expiryTime=_integer(client.get("expiryTime", 0)),
                tgId=_telegram_id(client.get("tgId", "")),
                comment=str(client.get("comment", "")),
                reset=_integer(client.get("reset", 0)),
                inboundIds=inbound_ids,
                traffic=ClientTraffic(
                    id=0,
                    inboundId=0,
                    enable=True,
                    email=email,
                    uuid=uuid_value,
                    subId=sub_id,
                    up=0,
                    down=0,
                    expiryTime=0,
                    total=0,
                    reset=0,
                ),
            )
        )
        return json_http({"success": True, "msg": "", "obj": None}, 200)

    def _post_bulk(self, url: str, body: dict[str, object]) -> Response:
        enable = "bulkEnable" in url
        raw_emails = body.get("emails", [])
        assert isinstance(raw_emails, list)
        emails = [str(e) for e in cast(list[object], raw_emails)]
        missing = [e for e in emails if self._find(e) is None]
        if missing:
            return json_http(
                {"success": False, "msg": f"client not found: {missing[0]}", "obj": None},
                200,
            )
        self.clients = [replace(c, enable=enable) if c.email in emails else c for c in self.clients]
        return json_http({"success": True, "msg": "", "obj": {"changed": len(emails)}}, 200)

    def _post_del(self, url: str, body: dict[str, object]) -> Response:
        del body
        email = unquote(url.rsplit("/", 1)[-1])
        found = self._find(email)
        if found is None:
            return json_http({"success": False, "msg": "client not found", "obj": None}, 200)
        self.clients.remove(found)
        return json_http({"success": True, "msg": "", "obj": None}, 200)

    def _post_update_traffic(self, url: str, body: dict[str, object]) -> Response:
        email = unquote(url.rsplit("/", 1)[-1])
        found = self._find(email)
        if found is None or found.traffic is None:
            return json_http({"success": False, "msg": "client not found", "obj": None}, 200)
        updated = replace(
            found,
            traffic=replace(
                found.traffic,
                up=_integer(body.get("upload", 0)),
                down=_integer(body.get("download", 0)),
            ),
        )
        self.clients = [updated if c.email == email else c for c in self.clients]
        return json_http({"success": True, "msg": "", "obj": None}, 200)

    def _post_update(self, url: str, body: dict[str, object]) -> Response:
        email = unquote(url.rsplit("/", 1)[-1])
        found = self._find(email)
        if found is None:
            return json_http({"success": False, "msg": "client not found", "obj": None}, 200)
        updated = replace(
            found,
            uuid=str(body.get("id", found.uuid)),
            subId=str(body.get("subId", found.subId)),
            enable=bool(body.get("enable", found.enable)),
            flow=str(body.get("flow", found.flow)),
            limitIp=_integer(body.get("limitIp", found.limitIp)),
            totalGB=_integer(body.get("totalGB", found.totalGB)),
            expiryTime=_integer(body.get("expiryTime", found.expiryTime)),
            tgId=_telegram_id(body.get("tgId", found.tgId)),
            comment=str(body.get("comment", found.comment)),
            reset=_integer(body.get("reset", found.reset)),
        )
        self.clients = [updated if c.email == email else c for c in self.clients]
        return json_http({"success": True, "msg": "", "obj": None}, 200)

    def _post_attach(self, url: str, body: dict[str, object]) -> Response:
        email = unquote(url.rsplit("/", 2)[-2])
        found = self._find(email)
        if found is None:
            return json_http({"success": False, "msg": "client not found", "obj": None}, 200)
        ids = set(_integer_list(body.get("inboundIds", [])))
        if "/attach" in url:
            merged = list(found.inboundIds) + sorted(ids - set(found.inboundIds))
            updated = replace(found, inboundIds=merged)
        else:
            updated = replace(found, inboundIds=[i for i in found.inboundIds if i not in ids])
        self.clients = [updated if c.email == email else c for c in self.clients]
        return json_http({"success": True, "msg": "", "obj": None}, 200)

    def _route_post(self, url: str, body: dict[str, object]) -> Response:
        for needle, handler in _POST_ROUTES:
            if needle in url:
                return handler(self, url, body)
        fallback: dict[str, JsonValue] = {"success": True, "obj": []}
        return json_http(fallback, self._post_status)


def _integer(value: object) -> int:
    assert isinstance(value, (int, float, str))
    return int(value)


def _integer_list(value: object) -> list[int]:
    assert isinstance(value, list)
    return [_integer(item) for item in cast(list[object], value)]


def _telegram_id(value: object) -> str | int:
    assert isinstance(value, (str, int))
    return value


_POST_ROUTES: tuple[tuple[str, _PostHandler], ...] = (
    ("clients/add", FakePanel._post_add),
    ("bulkEnable", FakePanel._post_bulk),
    ("bulkDisable", FakePanel._post_bulk),
    ("clients/del/", FakePanel._post_del),
    ("clients/updateTraffic/", FakePanel._post_update_traffic),
    ("clients/update/", FakePanel._post_update),
    ("/attach", FakePanel._post_attach),
    ("/detach", FakePanel._post_attach),
)
