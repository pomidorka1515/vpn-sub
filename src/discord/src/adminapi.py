from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from urllib.parse import urljoin

import aiohttp

from webapi import ApiResult

__all__ = ["AdminApiClient"]


class AdminApiClient:
    def __init__(
        self,
        base: str,
        uri: str,
        api_uri: str,
        token: str,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        parts = [part for part in (uri, api_uri) if part and part.strip("/")]
        prefix = "/".join(part.strip("/") for part in parts)
        root = base.rstrip("/")
        self._root = f"{root}/{prefix}" if prefix else root
        self._token = token
        self._session = session
        self._owns_session = session is None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
            self._owns_session = True

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _url(self, path: str) -> str:
        return urljoin(self._root + "/", path.lstrip("/"))

    def _session_or_raise(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            raise RuntimeError("AdminApiClient session is not started")
        return self._session

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._token}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, object] | None = None,
        params: Mapping[str, str | int] | None = None,
    ) -> ApiResult:
        session = self._session_or_raise()
        try:
            async with session.request(
                method,
                self._url(path),
                json=dict(json) if json is not None else None,
                params=dict(params) if params is not None else None,
                headers=self._headers(),
            ) as response:
                header_map = {str(key): str(value) for key, value in response.headers.items()}
                try:
                    payload = await response.json(content_type=None)
                except Exception:
                    return ApiResult(
                        ok=False,
                        status=response.status,
                        msg="http_unavailable",
                        obj=None,
                        raw_headers=header_map,
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
                )
        except aiohttp.ClientError:
            return ApiResult(
                ok=False,
                status=0,
                msg="http_unavailable",
                obj=None,
                raw_headers={},
            )

    async def list_users(self) -> ApiResult:
        return await self._request("GET", "/api/user/list")

    async def user_info(self, username: str, *, pretty: bool = True) -> ApiResult:
        params: dict[str, str | int] = {"user": username}
        if pretty:
            params["beautify"] = "1"
        return await self._request("GET", "/api/user/info", params=params)

    async def add_user(
        self,
        username: str,
        displayname: str,
        *,
        limit: int = 0,
        wl_limit: int = 5,
        timee: int = 0,
    ) -> ApiResult:
        return await self._request(
            "POST",
            "/api/user/add",
            json={
                "user": username,
                "displayname": displayname,
                "limit": limit,
                "wl_limit": wl_limit,
                "time": timee,
            },
        )

    async def delete_user(self, username: str, *, perma: bool = True) -> ApiResult:
        return await self._request(
            "POST",
            "/api/user/delete",
            json={"user": username, "perma": perma},
        )

    async def refresh(self) -> ApiResult:
        return await self._request("GET", "/api/user/refresh")

    async def onlines(self) -> ApiResult:
        return await self._request("GET", "/api/user/onlines", params={"keyed": "1"})

    async def reset_user(self, username: str) -> ApiResult:
        return await self._request("POST", "/api/user/reset", json={"user": username})

    async def update_user(
        self,
        username: str,
        *,
        fingerprint: str | None = None,
        displayname: str | None = None,
        limit: int | None = None,
        wl_limit: int | None = None,
        timee: int | None = None,
    ) -> ApiResult:
        payload: dict[str, object] = {"user": username}
        if fingerprint is not None:
            payload["fingerprint"] = fingerprint
        if displayname is not None:
            payload["displayname"] = displayname
        if limit is not None:
            payload["limit"] = limit
        if wl_limit is not None:
            payload["wl_limit"] = wl_limit
        if timee is not None:
            payload["time"] = timee
        return await self._request("POST", "/api/user/update", json=payload)

    async def history(self, username: str, days: int) -> ApiResult:
        return await self._request(
            "GET",
            "/api/user/history",
            params={"user": username, "days": days},
        )

    async def fingerprints(self) -> ApiResult:
        return await self._request("GET", "/api/fingerprints")

    async def panel_status(self) -> ApiResult:
        return await self._request("GET", "/api/panel/status")

    async def list_codes(self) -> ApiResult:
        return await self._request("GET", "/api/code/list")

    async def code_info(self, code: str) -> ApiResult:
        return await self._request("GET", "/api/code/info", params={"code": code})

    async def add_code(
        self,
        code: str,
        action: str,
        *,
        permanent: bool,
        days: int,
        gb: int,
        wl_gb: int,
        uses: int,
    ) -> ApiResult:
        return await self._request(
            "POST",
            "/api/code/add",
            json={
                "code": code,
                "action": action,
                "perma": permanent,
                "days": days,
                "gb": gb,
                "wl_gb": wl_gb,
                "uses": uses,
            },
        )

    async def delete_code(self, code: str) -> ApiResult:
        return await self._request("POST", "/api/code/delete", json={"code": code})

    async def leaderboard(
        self,
        category: str,
        cutoff: int,
        *,
        displaynames: bool = True,
        flip: bool = False,
    ) -> ApiResult:
        return await self._request(
            "POST",
            "/api/leaderboard",
            json={
                "type": category,
                "cutoff": cutoff,
                "displaynames": displaynames,
                "flip": flip,
            },
        )
