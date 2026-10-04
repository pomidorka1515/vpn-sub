"""Base64-encoded client link arrays."""

from __future__ import annotations

import urllib.parse
import json
import base64
from typing import Any
from collections import deque

from util import fmt_bytes
from custom_types import BandwidthInfo


def build_link_array(
    cfg: dict[str, Any],
    status: bool,
    statusWl: bool,
    lang: str,
    is_happ: bool,
    user_uuid: str,
    bandwidths: BandwidthInfo,
    need_dummy_link: bool,
    fingerprint: str,
    lang_cfg: dict[str, Any],
) -> str:
    """Build a base64-encoded link array."""
    generated_links: deque[str] = deque()

    for p_key, p_name in cfg['profiles'].items():
        if p_key in cfg['whitelistProfiles'] and not statusWl:
            continue
        if not status:
            break
        link: str = cfg['masterLinks'][p_key]
        flag: str = cfg['flags'][p_key] if is_happ else ""
        node: str = cfg['profileNodes'][p_key]
        domain: str = cfg['nodes'][node]
        name: str = flag + p_name[0 if lang == "en" else 1]
        link = link.replace("DOMAIN", domain)
        link = link.replace("FINGERPRINT", fingerprint)
        link = link.replace("UUID", user_uuid)
        link = link.replace("NAME", name)
        if "EXTRA" in link:
            extra_data = cfg['xhttpExtra'].get(p_key)
            if extra_data:
                json_str = json.dumps(extra_data, separators=(',', ':'))
                encoded_extra = urllib.parse.quote(json_str)
                link = link.replace("EXTRA", encoded_extra)
            else:
                link = link.replace("extra=EXTRA&", "").replace("&extra=EXTRA", "").replace("extra=EXTRA", "")
        generated_links.append(link)

    if need_dummy_link:
        dt = lang_cfg['description'][lang]['bw_label']
        dt = urllib.parse.quote(
            dt + "↑ {up} / ↓ {down}".format(
                up=fmt_bytes(bandwidths.upload),
                down=fmt_bytes(bandwidths.download)
            )
        )
        generated_links.appendleft(
            f"vless://0@localhost:1?type=tcp&security=none#{dt}"
        )

    raw_text = "\n".join(generated_links)

    return base64.b64encode(raw_text.encode('utf-8')).decode('utf-8')
