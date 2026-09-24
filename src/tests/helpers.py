from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any, cast
from urllib.parse import unquote

import json
import time
from flask import Flask
from requests import Response

from bwatch import BWatch
from config import ConfigLike
from core import Subscription
from custom_types import ClientTraffic, Inbound, PanelClient
from db import Database
from session import XUiSession

USER_UUID = "01234567-89ab-cdef-0123-456789abcdef"
TOKEN_A = "a" * 40


def subscription_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "uri": "sub",
        "fingerprints": ["chrome"],
        "salt": "test-salt",
        "domain": "https://example.test",
        "funny_strings": ["test"],
    }
    config.update(overrides)
    return config


def make_subscription(
    database: Database,
    *,
    panels: list[object] | None = None,
    whitelist_panel: object | None = None,
    app: Flask | None = None,
    audit_cfg: object | None = None,
    **config_overrides: Any,
) -> Subscription:
    return Subscription(
        cfg=cast(ConfigLike, subscription_config(**config_overrides)),
        lang_cfg=cast(ConfigLike, {}),
        db=database,
        app=app or Flask(__name__),
        panels=cast(list[XUiSession], panels or []),
        whitelist_panel=cast(XUiSession | None, whitelist_panel),
        audit_cfg=cast(Any, audit_cfg),
    )


def create_alice(database: Database, **kwargs: Any) -> None:
    payload: dict[str, Any] = {
        "username": "alice",
        "uuid": USER_UUID,
        "token": TOKEN_A,
        "fingerprint": "chrome",
        "displayname": "Alice",
    }
    payload.update(kwargs)
    database.create_user(**payload)


class BWatchConfig:
    def get(self, key: str, *args: object, **kwargs: object) -> object:
        if key == "panel_alert_cooldown":
            return 3600
        raise KeyError(key)


def make_watch(
    database: Database,
    subscription: Subscription,
    *,
    bot: object | None = None,
    admin_bot: object | None = None,
) -> BWatch:
    return BWatch(
        cfg=cast(ConfigLike, BWatchConfig()),
        db=database,
        sub=subscription,
        bot=cast(Any, bot),
        admin_bot=cast(Any, admin_bot),
    )


def json_http(data: dict[str, Any] | list[Any], status_code: int = 200) -> Response:
    response = Response()
    response.status_code = status_code
    response._content = json.dumps(data).encode("utf-8")
    response.headers["Content-Type"] = "application/json"
    return response


def make_client(uuid: str, up: int, down: int, inbound_id: int = 1) -> ClientTraffic:
    return ClientTraffic(
        id=inbound_id, inboundId=inbound_id, enable=True, email="alice",
        uuid=uuid, subId="", up=up, down=down,
        expiryTime=0, total=0, reset=0, lastOnline=0,
    )


def make_panel_client(
    email: str,
    inbound_ids: list[int],
    up: int = 0,
    down: int = 0,
    uuid: str = USER_UUID,
) -> PanelClient:
    return PanelClient(
        email=email, uuid=uuid, subId="", enable=True, flow="",
        limitIp=0, totalGB=0, expiryTime=0, tgId="", comment="", reset=0,
        inboundIds=inbound_ids,
        traffic=ClientTraffic(
            id=0, inboundId=0, enable=True, email=email, uuid=uuid,
            subId="", up=up, down=down, expiryTime=0, total=0, reset=0,
        ),
    )


def make_inbound(
    inbound_id: int,
    clients: list[ClientTraffic] | None = None,
    protocol: str = "vless",
) -> Inbound:
    return Inbound(
        id=inbound_id, up=0, down=0, total=0, remark="test", enable=True,
        expiryTime=0, trafficReset="", lastTrafficResetTime=0, clientStats=clients or [],
        listen="", port=443, protocol=protocol, settings="{}",
        streamSettings=json.dumps({"network": "tcp"}), tag="", sniffing="",
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
        post_payload: dict[str, Any] | None = None,
        post_status: int = 200,
        post_error: BaseException | None = None,
        post_queue: list[dict[str, Any] | BaseException] | None = None,
        get_payload: dict[str, Any] | None = None,
        get_status: int = 200,
        get_error: BaseException | None = None,
        status_payload: dict[str, Any] | None = None,
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

    @property
    def cache_age(self) -> float:
        if self._cache is None:
            return float("inf")
        return time.monotonic() - self._cache_set_at

    def clear_cache(self) -> None:
        self.cache = None

    def close(self) -> None:
        pass

    def get(self, url: str) -> Response:
        self.gets.append(url)
        if "server/status" in url:
            if self._status_error is not None:
                raise self._status_error
            payload: dict[str, Any] = (
                self._status_payload if self._status_payload is not None
                else {"success": True, "msg": "", "obj": {}}
            )
            return json_http(payload, self._status_status)
        if self._get_error is not None:
            raise self._get_error
        if self._get_payload is not None:
            return json_http(self._get_payload, self._get_status)
        if "clients/list" in url:
            return json_http({
                "success": True,
                "msg": "",
                "obj": [asdict(client) for client in self.clients],
            }, self._get_status)
        if "clients/get/" in url:
            email = unquote(url.rsplit("/", 1)[-1])
            found = next((c for c in self.clients if c.email == email), None)
            if found is None:
                return json_http({"success": True, "msg": "", "obj": None}, 200)
            return json_http(
                {"success": True, "msg": "", "obj": asdict(found)}, self._get_status,
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
        return json_http({
            "success": True,
            "msg": "",
            "obj": [asdict(inbound) for inbound in self._inbounds],
        }, self._get_status)

    def post(self, url: str, **kwargs: object) -> Response:
        self.posts.append((url, dict(kwargs)))
        if self._post_error is not None:
            raise self._post_error
        if self._post_queue:
            item = self._post_queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            queued = dict(item)
            status = int(queued.pop("status_code", self._post_status))
            return json_http(queued, status)
        if self._post_payload is not None:
            return json_http(self._post_payload, self._post_status)
        raw_body: object = kwargs.get("json")
        body: dict[str, Any] = dict(cast(dict[str, Any], raw_body)) if isinstance(raw_body, dict) else {}
        return self._route_post(url, body)

    def _find(self, email: str) -> PanelClient | None:
        return next((c for c in self.clients if c.email == email), None)

    def _route_post(self, url: str, body: dict[str, Any]) -> Response:
        if "clients/add" in url:
            raw: object = body.get("client")
            if not isinstance(raw, dict):
                return json_http({"success": False, "msg": "missing client", "obj": None}, 200)
            client = cast(dict[str, Any], raw)
            email = str(client.get("email", ""))
            if self._find(email) is not None:
                return json_http({"success": False, "msg": "duplicate email", "obj": None}, 200)
            inbound_ids = [int(i) for i in cast(list[Any], body.get("inboundIds", []))]
            uuid_value = str(client.get("id", ""))
            sub_id = str(client.get("subId", ""))
            self.clients.append(PanelClient(
                email=email, uuid=uuid_value, subId=sub_id,
                enable=bool(client.get("enable", True)),
                flow=str(client.get("flow", "")),
                limitIp=int(client.get("limitIp", 0)),
                totalGB=int(client.get("totalGB", 0)),
                expiryTime=int(client.get("expiryTime", 0)),
                tgId=client.get("tgId", ""),
                comment=str(client.get("comment", "")),
                reset=int(client.get("reset", 0)),
                inboundIds=inbound_ids,
                traffic=ClientTraffic(
                    id=0, inboundId=0, enable=True, email=email, uuid=uuid_value,
                    subId=sub_id, up=0, down=0, expiryTime=0, total=0, reset=0,
                ),
            ))
            return json_http({"success": True, "msg": "", "obj": None}, 200)
        if "bulkEnable" in url or "bulkDisable" in url:
            enable = "bulkEnable" in url
            emails = [str(e) for e in cast(list[Any], body.get("emails", []))]
            missing = [e for e in emails if self._find(e) is None]
            if missing:
                return json_http(
                    {"success": False, "msg": f"client not found: {missing[0]}", "obj": None},
                    200,
                )
            self.clients = [
                replace(c, enable=enable) if c.email in emails else c
                for c in self.clients
            ]
            return json_http({"success": True, "msg": "", "obj": {"changed": len(emails)}}, 200)
        if "clients/del/" in url:
            email = unquote(url.rsplit("/", 1)[-1])
            found = self._find(email)
            if found is None:
                return json_http({"success": False, "msg": "client not found", "obj": None}, 200)
            self.clients.remove(found)
            return json_http({"success": True, "msg": "", "obj": None}, 200)
        if "clients/updateTraffic/" in url:
            email = unquote(url.rsplit("/", 1)[-1])
            found = self._find(email)
            if found is None or found.traffic is None:
                return json_http({"success": False, "msg": "client not found", "obj": None}, 200)
            updated = replace(
                found,
                traffic=replace(
                    found.traffic,
                    up=int(body.get("upload", 0)),
                    down=int(body.get("download", 0)),
                ),
            )
            self.clients = [updated if c.email == email else c for c in self.clients]
            return json_http({"success": True, "msg": "", "obj": None}, 200)
        if "clients/update/" in url:
            email = unquote(url.rsplit("/", 1)[-1])
            found = self._find(email)
            if found is None:
                return json_http({"success": False, "msg": "client not found", "obj": None}, 200)
            client = body
            updated = replace(
                found,
                uuid=str(client.get("id", found.uuid)),
                subId=str(client.get("subId", found.subId)),
                enable=bool(client.get("enable", found.enable)),
                flow=str(client.get("flow", found.flow)),
                limitIp=int(client.get("limitIp", found.limitIp)),
                totalGB=int(client.get("totalGB", found.totalGB)),
                expiryTime=int(client.get("expiryTime", found.expiryTime)),
                tgId=client.get("tgId", found.tgId),
                comment=str(client.get("comment", found.comment)),
                reset=int(client.get("reset", found.reset)),
            )
            self.clients = [updated if c.email == email else c for c in self.clients]
            return json_http({"success": True, "msg": "", "obj": None}, 200)
        if "/attach" in url or "/detach" in url:
            email = unquote(url.rsplit("/", 2)[-2])
            found = self._find(email)
            if found is None:
                return json_http({"success": False, "msg": "client not found", "obj": None}, 200)
            ids = {int(i) for i in cast(list[Any], body.get("inboundIds", []))}
            if "/attach" in url:
                merged = list(found.inboundIds) + sorted(ids - set(found.inboundIds))
                updated = replace(found, inboundIds=merged)
            else:
                updated = replace(found, inboundIds=[i for i in found.inboundIds if i not in ids])
            self.clients = [updated if c.email == email else c for c in self.clients]
            return json_http({"success": True, "msg": "", "obj": None}, 200)
        fallback: dict[str, Any] = {"success": True, "obj": []}
        return json_http(fallback, self._post_status)
