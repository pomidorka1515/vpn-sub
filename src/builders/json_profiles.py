"""Happ JSON profile arrays."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from config.snapshot import thaw

if TYPE_CHECKING:
    from config import AppConfig


@dataclass(frozen=True, slots=True)
class _JsonTemplate:
    whitelist: bool
    # Literal JSON fragments alternate with indexes into the personalized values.
    parts: tuple[str | int, ...]


_templates: dict[int, tuple[object, tuple[tuple[_JsonTemplate, ...], ...]]] = {}
_MAX_CACHED_CONFIGS = 8


def _build_templates(cfg: AppConfig) -> tuple[tuple[_JsonTemplate, ...], ...]:
    variants: list[list[_JsonTemplate]] = [[], []]
    for profile in cfg["profiles"].values():
        node = profile["node"]
        domain = cfg["nodes"][node]
        outbound = thaw(profile["json"])
        vnext = outbound["settings"]["vnext"][0]
        vnext["address"] = domain

        stream = outbound["streamSettings"]
        if (tls := stream.get("tlsSettings")) is not None:
            tls["serverName"] = domain
        reality = stream.get("realitySettings")
        is_reality = reality is not None
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

        for index in (0, 1):
            result = thaw(cfg["json_template"])
            result["remarks"] = profile["flag"] + profile["name"][index]
            result["outbounds"][0] = outbound
            # Only shown when a provider id is set, but written regardless.
            meta = result.setdefault("meta", {})
            meta["serverDescription"] = profile["shortProfileDescription"][index]

            # Pick markers absent from all configured JSON, including arbitrary extras.
            original = json.dumps(result, ensure_ascii=False)
            while True:
                markers = (f"__uuid_{uuid4().hex}__", f"__fingerprint_{uuid4().hex}__")
                if all(marker not in original for marker in markers):
                    break
            vnext["users"][0]["id"] = markers[0]
            if tls is not None:
                tls["fingerprint"] = markers[1]
            if reality is not None:
                reality["fingerprint"] = markers[1]
            serialized = json.dumps(result, ensure_ascii=False)
            quoted = tuple(json.dumps(marker) for marker in markers)
            pattern = re.compile("(" + "|".join(map(re.escape, quoted)) + ")")
            parts: tuple[str | int, ...] = tuple(
                quoted.index(part) if part in quoted else part for part in pattern.split(serialized)
            )
            variants[index].append(_JsonTemplate(profile["whitelist"], parts))

    return tuple(tuple(profiles) for profiles in variants)


def _template_variants(cfg: AppConfig) -> tuple[tuple[_JsonTemplate, ...], ...]:
    if not isinstance(cast(object, cfg), MappingProxyType):
        return _build_templates(cfg)
    key = id(cfg)
    cached = _templates.get(key)
    if cached is not None and cached[0] is cfg:
        return cached[1]
    result = _build_templates(cfg)
    keys = list(_templates)
    if len(keys) >= _MAX_CACHED_CONFIGS:
        _templates.pop(keys[0], None)
    _templates[key] = (cfg, result)
    return result


def build_json(
    cfg: AppConfig,
    user_uuid: str,
    lang: str,
    fingerprint: str,
    status: bool = True,
    statusWl: bool = True,  # ruff: ignore[invalid-argument-name]
) -> str:
    """Build a serialized array of personalized profiles for Happ."""
    if not status:
        return "[]"
    profiles = _template_variants(cfg)[0 if lang == "en" else 1]
    values = (
        json.dumps(user_uuid, ensure_ascii=False),
        json.dumps(fingerprint, ensure_ascii=False),
    )
    rendered = (
        "".join(values[part] if isinstance(part, int) else part for part in profile.parts)
        for profile in profiles
        if statusWl or not profile.whitelist
    )
    return "[" + ", ".join(rendered) + "]"
