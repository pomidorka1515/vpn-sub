"""Happ JSON profile arrays."""

from __future__ import annotations

import copy
from typing import Any


def build_json(
    cfg: dict[str, Any],
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

    for key, names in cfg["profiles"].items():
        node = cfg["profileNodes"][key]
        domain = cfg["nodes"][node]
        outbound: dict[str, Any] = cfg["json_profiles"][key]
        vnext: dict[str, Any] = outbound["settings"]["vnext"][0]
        vnext["users"][0]["id"] = user_uuid
        vnext["address"] = domain

        stream: dict[str, Any] = outbound["streamSettings"]
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

        result: dict[str, Any] = copy.deepcopy(cfg["json_template"])
        result["remarks"] = cfg["flags"][key] + names[index]
        result["outbounds"][0] = outbound
        # only shown when a provider id is set, but written regardless
        meta = result.setdefault("meta", {})
        meta["serverDescription"] = cfg["shortProfileDescriptions"][key][index]
        profiles.append(result)

    return profiles
