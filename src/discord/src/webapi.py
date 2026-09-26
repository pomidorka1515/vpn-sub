from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from urllib.parse import urljoin

import aiohttp

__all__ = ["ApiResult", "WebApiClient", "extract_auth_token"]

_STATS_TTL_S = 20.0
_AUTH_COOKIE = "auth_token="


@dataclass(frozen=True, slots=True)
class ApiResult:
    ok: bool
    status: int
    msg: str | None
    obj: object
    raw_headers: Mapping[str, str]
    body: bytes | None = None
    auth_token: str | None = None


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
        self._stats_cache: dict[str, tuple[float, ApiResult]] = {}

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

    async def stats(self, token: str) -> ApiResult:
        cached = self._stats_cache.get(token)
        now = time.monotonic()
        if cached is not None and now - cached[0] < _STATS_TTL_S:
            return cached[1]
        result = await self._request("GET", "/stats", token=token)
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

    async def fingerprints(self, token: str) -> ApiResult:
        return await self._request("GET", "/fingerprints", token=token)

    async def delete(self, token: str, current_password: str) -> ApiResult:
        self.invalidate_stats(token)
        return await self._request(
            "POST",
            "/delete",
            token=token,
            json={"current_password": current_password},
        )

    async def validate_username(self, username: str) -> ApiResult:
        return await self._request("GET", "/validate", params={"username": username})

    async def profiles(self, token: str, lang: str) -> ApiResult:
        return await self._request("GET", "/profiles", token=token, params={"lang": lang})

    async def history(self, token: str, days: int) -> ApiResult:
        return await self._request("GET", "/history", token=token, params={"days": days})

    async def qr(self, token: str, *, happ: bool, lang: str) -> ApiResult:
        params: dict[str, str | int] = {"lang": lang}
        if happ:
            params["happ"] = 1
        return await self._request("GET", "/qr", token=token, params=params, expect_image=True)
