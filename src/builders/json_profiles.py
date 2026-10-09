"""Happ JSON profile arrays."""

from __future__ import annotations

import copy
from typing import NotRequired, TypedDict, cast
from config import AppConfig


class _User(TypedDict):
    id: str


class _VNext(TypedDict):
    users: list[_User]
    address: str


class _Settings(TypedDict):
    vnext: list[_VNext]


class _TLS(TypedDict):
    serverName: str
    fingerprint: str


class _Reality(TypedDict):
    fingerprint: str


class _Host(TypedDict):
    host: str


class _GRPC(TypedDict):
    authority: str


class _WS(_Host):
    headers: NotRequired[dict[str, str]]


class _Stream(TypedDict, total=False):
    tlsSettings: _TLS
    realitySettings: _Reality
    xhttpSettings: _Host
    grpcSettings: _GRPC
    wsSettings: _WS
    httpupgradeSettings: _Host


class _Outbound(TypedDict):
    settings: _Settings
    streamSettings: _Stream


class _Template(TypedDict):
    remarks: str
    outbounds: list[_Outbound]
    meta: NotRequired[dict[str, str]]


def build_json(
    cfg: AppConfig,
    user_uuid: str,
    lang: str,
    fingerprint: str,
) -> list[dict[str, object]]:
    """Build an array of profiles for Happ.

    ``cfg`` is already an independent copy, so each outbound is filled in
    place. The template is copied per profile because remarks and meta differ.
    """
    index = 0 if lang == "en" else 1
    profiles: list[dict[str, object]] = []

    for profile in cfg["profiles"].values():
        node = profile["node"]
        domain = cfg["nodes"][node]
        outbound = cast(_Outbound, profile["json"])
        vnext = outbound["settings"]["vnext"][0]
        vnext["users"][0]["id"] = user_uuid
        vnext["address"] = domain

        stream = outbound["streamSettings"]
        if (tls := stream.get("tlsSettings")) is not None:
            tls["serverName"] = domain
            tls["fingerprint"] = fingerprint
        if (reality := stream.get("realitySettings")) is not None:
            is_reality = True
            reality["fingerprint"] = fingerprint
        else:
            is_reality = False
        if not is_reality and (xhttp := stream.get("xhttpSettings")) is not None:
            xhttp["host"] = domain
        if not is_reality and (grpc := stream.get("grpcSettings")) is not None:
            grpc["authority"] = domain
        if (ws := stream.get("wsSettings")) is not None:
            headers = ws.setdefault("headers", {})
            ws["host"] = domain
            headers["Host"] = domain
        if (upgrade := stream.get("httpupgradeSettings")) is not None:
            upgrade["host"] = domain

        result = cast(_Template, copy.deepcopy(cfg["json_template"]))
        result["remarks"] = profile["flag"] + profile["name"][index]
        result["outbounds"][0] = outbound
        # only shown when a provider id is set, but written regardless
        meta = result.setdefault("meta", {})
        meta["serverDescription"] = profile["shortProfileDescription"][index]
        profiles.append(dict(result))

    return profiles
