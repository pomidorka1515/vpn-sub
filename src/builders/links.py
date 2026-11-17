"""Base64-encoded client link arrays."""

from __future__ import annotations

import base64
import json
import urllib.parse
from collections import deque
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from config.snapshot import thaw
from util import fmt_bytes

if TYPE_CHECKING:
    from config import AppConfig, LangConfig
    from custom_types import BandwidthInfo


@dataclass(frozen=True, slots=True)
class _LinkTemplate:
    whitelist: bool
    domain: str
    link: str


# Config snapshots are immutable and are reused for their whole generation.
# Keep templates, not rendered per-user arrays, so this cache stays small.
_templates: dict[int, tuple[object, tuple[tuple[_LinkTemplate, ...], ...]]] = {}


def _build_templates(cfg: AppConfig) -> tuple[tuple[_LinkTemplate, ...], ...]:
    variants: list[tuple[_LinkTemplate, ...]] = []
    for lang_index in (0, 1):
        for is_happ in (False, True):
            profiles: list[_LinkTemplate] = []
            for profile in cfg["profiles"].values():
                link = profile["masterLink"]
                name = (profile["flag"] if is_happ else "") + profile["name"][lang_index]
                link = link.replace("NAME", urllib.parse.quote(name, safe=""))
                if "EXTRA" in link:
                    extra_data = profile["xhttpExtra"]
                    if extra_data:
                        encoded_extra = urllib.parse.quote(
                            json.dumps(thaw(extra_data), separators=(",", ":"))
                        )
                        link = link.replace("EXTRA", encoded_extra)
                    else:
                        link = (
                            link.replace("extra=EXTRA&", "")
                            .replace("&extra=EXTRA", "")
                            .replace("extra=EXTRA", "")
                        )
                profiles.append(
                    _LinkTemplate(profile["whitelist"], cfg["nodes"][profile["node"]], link)
                )
            variants.append(tuple(profiles))
    return tuple(variants)


def _template_variants(cfg: AppConfig) -> tuple[tuple[_LinkTemplate, ...], ...] | None:
    if not isinstance(cast(object, cfg), MappingProxyType):
        return None
    key = id(cfg)
    cached = _templates.get(key)
    if cached is not None and cached[0] is cfg:
        return cached[1]
    result = _build_templates(cfg)
    _templates[key] = (cfg, result)
    return result


def build_link_array(
    cfg: AppConfig,
    status: bool,
    statusWl: bool,  # ruff: ignore[invalid-argument-name]
    lang: str,
    is_happ: bool,
    user_uuid: str,
    bandwidths: BandwidthInfo,
    need_dummy_link: bool,
    fingerprint: str,
    lang_cfg: LangConfig,
) -> str:
    """Build a base64-encoded link array."""
    generated_links: deque[str] = deque()
    templates = _template_variants(cfg)
    if templates is None:
        templates = _build_templates(cfg)
    profiles = templates[(0 if lang == "en" else 1) * 2 + int(is_happ)]

    for profile in profiles:
        if profile.whitelist and not statusWl:
            continue
        if not status:
            break
        link = profile.link
        # These are the only values that vary between users.
        link = link.replace("DOMAIN", profile.domain)
        link = link.replace("FINGERPRINT", fingerprint)
        link = link.replace("UUID", user_uuid)
        generated_links.append(link)

    if need_dummy_link:
        dt = lang_cfg["description"][lang]["bw_label"]
        dt = urllib.parse.quote(
            dt + f"↑ {fmt_bytes(bandwidths.upload)} / ↓ {fmt_bytes(bandwidths.download)}",
        )
        generated_links.appendleft(f"vless://0@localhost:1?type=tcp&security=none#{dt}")

    raw_text = "\n".join(generated_links)

    return base64.b64encode(raw_text.encode("utf-8")).decode("utf-8")
