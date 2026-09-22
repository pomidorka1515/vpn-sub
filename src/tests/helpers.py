from __future__ import annotations

from dataclasses import asdict
from typing import Any, cast

import json
from flask import Flask
from requests import Response

from bwatch import BWatch
from config import ConfigLike
from core import Subscription
from custom_types import ClientStats, Inbound
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


def make_watch(database: Database, subscription: Subscription) -> BWatch:
    return BWatch(
        cfg=cast(ConfigLike, BWatchConfig()),
        db=database,
        sub=subscription,
        bot=None,
        admin_bot=None,
    )


def json_http(data: dict[str, Any] | list[Any], status_code: int = 200) -> Response:
    response = Response()
    response.status_code = status_code
    response._content = json.dumps(data).encode("utf-8")
    response.headers["Content-Type"] = "application/json"
    return response


def make_client(uuid: str, up: int, down: int, inbound_id: int = 1) -> ClientStats:
    return ClientStats(
        id=inbound_id, inboundId=inbound_id, enable=True, email="alice-abcd1234",
        uuid=uuid, subId="", up=up, down=down, allTime=up + down,
        expiryTime=0, total=0, reset=0, lastOnline=0,
    )


def make_inbound(inbound_id: int, clients: list[ClientStats] | None = None) -> Inbound:
    return Inbound(
        id=inbound_id, up=0, down=0, total=0, allTime=0, remark="test", enable=True,
        expiryTime=0, trafficReset="", lastTrafficResetTime=0, clientStats=clients or [],
        listen="", port=443, protocol="vless", settings="{}",
        streamSettings=json.dumps({"network": "tcp"}), tag="", sniffing="",
    )


class FakePanel:
    def __init__(
        self,
        *,
        inbounds: list[Inbound] | None = None,
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
        self.cache: list[Inbound] | None = None
        self.cache_time = 0
        self.name = name
        self._inbounds = inbounds or []
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

    def clear_cache(self) -> None:
        self.cache = None
        self.cache_time = 0

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
        fallback: dict[str, Any] = (
            self._post_payload if self._post_payload is not None
            else {"success": True, "obj": []}
        )
        return json_http(fallback, self._post_status)
