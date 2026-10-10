from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypedDict, cast
from urllib.parse import urljoin

import aiohttp

from custom_types import BandwidthSnapshotPayload

__all__ = ["ApiResult", "WebApiClient", "extract_auth_token"]

_STATS_TTL_S = 20.0
_AUTH_COOKIE = "auth_token="


@dataclass(frozen=True, slots=True)
class ApiResult[T = object]:
    ok: bool
    status: int
    msg: str | None
    obj: T
    raw_headers: Mapping[str, str]
    body: bytes | None = None
    auth_token: str | None = None


class BandwidthTotal(TypedDict, total=False):
    upload: int | float
    download: int | float
    total: int | float


class StatsBandwidth(TypedDict, total=False):
    total: BandwidthTotal
    wl_total: BandwidthTotal
    monthly: int | float
    wl_monthly: int | float
    limit: int
    wl_limit: int


class StatsPayload(TypedDict, total=False):
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
    bandwidth: StatsBandwidth


class UsernamePayload(TypedDict):
    valid: bool
    taken: bool
    sanitized: str


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return cast(dict[str, object], value)


def _stats(value: object) -> StatsPayload:
    obj = _mapping(value)
    for key, item in obj.items():
        if key in ("_", "token", "link", "displayname", "uuid", "fingerprint"):
            if not isinstance(item, str):
                raise ValueError(key)
        elif key in ("enabled", "wl_enabled", "online"):
            if not isinstance(item, bool):
                raise ValueError(key)
        elif key == "time":
            if not isinstance(item, int):
                raise ValueError(key)
        elif key == "bandwidth":
            for field, amount in _mapping(item).items():
                if field in ("total", "wl_total"):
                    for number in _mapping(amount).values():
                        if not isinstance(number, (int, float)):
                            raise ValueError(field)
                elif field in ("monthly", "wl_monthly", "limit", "wl_limit"):
                    if not isinstance(amount, (int, float)) or (field in ("limit", "wl_limit") and not isinstance(amount, int)):
                        raise ValueError(field)
    return cast(StatsPayload, obj)


def _strings(value: object) -> dict[str, str]:
    obj = _mapping(value)
    if not all(isinstance(item, str) for item in obj.values()):
        raise ValueError("expected string values")
    return cast(dict[str, str], obj)


def _fingerprints(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in cast(list[object], value)):
        raise ValueError("expected strings")
    return cast(list[str], value)


def _history(value: object) -> list[BandwidthSnapshotPayload]:
    if not isinstance(value, list):
        raise ValueError("expected history rows")
    rows = cast(list[object], value)
    for row in rows:
        obj = _mapping(row)
        if not all(isinstance(obj.get(key), int) for key in ("ts", "up", "down", "wl_up", "wl_down")):
            raise ValueError("invalid history row")
    return cast(list[BandwidthSnapshotPayload], rows)


def _username(value: object) -> UsernamePayload:
    obj = _mapping(value)
    if not isinstance(obj.get("valid"), bool) or not isinstance(obj.get("taken"), bool) or not isinstance(obj.get("sanitized"), str):
        raise ValueError("invalid username result")
    return cast(UsernamePayload, obj)


def _parsed[T](result: ApiResult, parser: Callable[[object], T]) -> ApiResult[T | None]:
    obj: T | None = None
    ok, msg = result.ok, result.msg
    if ok:
        try:
            obj = parser(result.obj)
        except ValueError:
            ok, msg = False, "bad_response"
    return ApiResult(ok, result.status, msg, obj, result.raw_headers, result.body, result.auth_token)


def _token_from_set_cookie(header: str) -> str | None:
    for part in header.split(";"):
        piece = part.strip()
        if piece.lower().startswith(_AUTH_COOKIE):
            value = piece.split("=", 1)[1]
            return value or None
    return None


def extract_auth_token(headers: Mapping[str, str]) -> str | None:
    values: list[str] = []
    getall = getattr(headers, "getall", None)
    if callable(getall):
        for key in ("Set-Cookie", "set-cookie"):
            raw = getall(key, ())
            if isinstance(raw, (list, tuple)):
                cookies = cast(list[str] | tuple[str, ...], raw)
                for item in cookies:
                    if item not in values:
                        values.append(item)
    for key in ("Set-Cookie", "set-cookie"):
        value = headers.get(key)
        if isinstance(value, str) and value not in values:
            values.append(value)
    for item in values:
        token = _token_from_set_cookie(item)
        if token:
            return token
    return None


class WebApiClient:
    def __init__(
        self,
        base: str,
        uri: str,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        prefix = "/".join(part for part in uri.split("/") if part)
        root = base.rstrip("/")
        self._root = f"{root}/{prefix}/webapi" if prefix else f"{root}/webapi"
        self._session = session
        self._owns_session = session is None
        self._stats_cache: dict[str, tuple[float, ApiResult[StatsPayload | None]]] = {}

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
            self._owns_session = True

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def invalidate_stats(self, token: str) -> None:
        self._stats_cache.pop(token, None)

    def _url(self, path: str) -> str:
        return urljoin(self._root + "/", path.lstrip("/"))

    def _session_or_raise(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            raise RuntimeError("WebApiClient session is not started")
        return self._session

    def _headers(self, token: str | None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if token:
            headers["Cookie"] = f"auth_token={token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json: Mapping[str, object] | None = None,
        params: Mapping[str, str | int] | None = None,
        expect_image: bool = False,
    ) -> ApiResult:
        session = self._session_or_raise()
        try:
            async with session.request(
                method,
                self._url(path),
                json=dict(json) if json is not None else None,
                params=dict(params) if params is not None else None,
                headers=self._headers(token),
            ) as response:
                header_map = {str(key): str(value) for key, value in response.headers.items()}
                auth_token = extract_auth_token(cast(Mapping[str, str], response.headers))
                content_type = (response.content_type or "").lower()
                if expect_image or content_type.startswith("image/"):
                    body = await response.read()
                    ok = 200 <= response.status < 300
                    return ApiResult(
                        ok=ok,
                        status=response.status,
                        msg=None if ok else "qr failed",
                        obj=None,
                        raw_headers=header_map,
                        body=body if ok else None,
                        auth_token=auth_token,
                    )
                try:
                    payload = await response.json(content_type=None)
                except Exception:
                    return ApiResult(
                        ok=False,
                        status=response.status,
                        msg="http_unavailable",
                        obj=None,
                        raw_headers=header_map,
                        auth_token=auth_token,
                    )
                typed = cast(dict[str, object], payload)
                success = bool(typed.get("success"))
                msg_raw = typed.get("msg")
                msg = msg_raw if isinstance(msg_raw, str) else None
                return ApiResult(
                    ok=success and 200 <= response.status < 300,
                    status=response.status,
                    msg=msg,
                    obj=typed.get("obj"),
                    raw_headers=header_map,
                    auth_token=auth_token,
                )
        except aiohttp.ClientError:
            return ApiResult(
                ok=False,
                status=0,
                msg="http_unavailable",
                obj=None,
                raw_headers={},
            )

    async def register(
        self,
        username: str,
        password: str,
        code: str,
        name: str,
    ) -> ApiResult:
        created = await self._request(
            "POST",
            "/register",
            json={
                "username": username,
                "password": password,
                "code": code,
                "name": name,
            },
        )
        if not created.ok:
            return created
        return await self.login(username, password)

    async def login(self, username: str, password: str) -> ApiResult:
        return await self._request(
            "POST",
            "/login",
            json={"username": username, "password": password},
        )

    async def stats(self, token: str) -> ApiResult[StatsPayload | None]:
        cached = self._stats_cache.get(token)
        now = time.monotonic()
        if cached is not None and now - cached[0] < _STATS_TTL_S:
            return cached[1]
        result = _parsed(await self._request("GET", "/stats", token=token), _stats)
        if result.ok:
            self._stats_cache[token] = (now, result)
        return result

    async def bonus(self, token: str, code: str) -> ApiResult:
        self.invalidate_stats(token)
        return await self._request("POST", "/bonus", token=token, json={"code": code})

    async def reset(self, token: str) -> ApiResult:
        self.invalidate_stats(token)
        return await self._request("POST", "/reset", token=token)

    async def settings(self, token: str, **fields: str) -> ApiResult:
        self.invalidate_stats(token)
        payload = {key: value for key, value in fields.items() if value}
        return await self._request("POST", "/settings", token=token, json=payload)

    async def logout(self, token: str) -> ApiResult:
        self.invalidate_stats(token)
        return await self._request("POST", "/logout", token=token)

    async def fingerprints(self, token: str) -> ApiResult[list[str] | None]:
        return _parsed(await self._request("GET", "/fingerprints", token=token), _fingerprints)

    async def delete(self, token: str, current_password: str) -> ApiResult:
        self.invalidate_stats(token)
        return await self._request(
            "POST",
            "/delete",
            token=token,
            json={"current_password": current_password},
        )

    async def validate_username(self, username: str) -> ApiResult[UsernamePayload | None]:
        return _parsed(await self._request("GET", "/validate", params={"username": username}), _username)

    async def profiles(self, token: str, lang: str) -> ApiResult[dict[str, str] | None]:
        return _parsed(await self._request("GET", "/profiles", token=token, params={"lang": lang}), _strings)

    async def history(self, token: str, days: int) -> ApiResult[list[BandwidthSnapshotPayload] | None]:
        return _parsed(await self._request("GET", "/history", token=token, params={"days": days}), _history)

    async def qr(self, token: str, *, happ: bool, lang: str) -> ApiResult:
        params: dict[str, str | int] = {"lang": lang}
        if happ:
            params["happ"] = 1
        return await self._request("GET", "/qr", token=token, params=params, expect_image=True)
