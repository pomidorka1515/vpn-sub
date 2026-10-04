"""Happ JSON profile arrays."""

from __future__ import annotations

import copy
from typing import Any, Literal


def build_json(
    cfg: dict[str, Any],
    user_uuid: str,
    lang: str,
    fingerprint: str,
) -> list[dict[str, object]]:
    """Build an array of profiles for Happ."""
    obj: list[dict[str, object]] = []
    template: dict[str, object] = cfg['json_template']
    index: Literal[0, 1] = 0 if lang == "en" else 1 # language index

    
    for p_key, p_name_list in cfg['profiles'].items():
        flag: str = cfg['flags'][p_key]
        p_name_raw: str = p_name_list[index]
        p_name: str = flag + p_name_raw
        node: str = cfg['profileNodes'][p_key]
        domain: str = cfg['nodes'][node]
        short_desc: str = cfg['shortProfileDescriptions'][p_key][index]
        is_reality: bool = False
        result: dict[str, Any] = copy.deepcopy(template)
        result['remarks'] = p_name
        result['outbounds'][0] = cfg['json_profiles'][p_key]
        result['outbounds'][0]['settings']['vnext'][0]['users'][0]['id'] = user_uuid
        result['outbounds'][0]['settings']['vnext'][0]['address'] = domain
        if result['outbounds'][0]['streamSettings'].get('tlsSettings', None) is not None:
            result['outbounds'][0]['streamSettings']['tlsSettings']['serverName'] = domain
            result['outbounds'][0]['streamSettings']['tlsSettings']['fingerprint'] = fingerprint
        if result['outbounds'][0]['streamSettings'].get('realitySettings', None) is not None:
            is_reality = True
            result['outbounds'][0]['streamSettings']['realitySettings']['fingerprint'] = fingerprint
        if result['outbounds'][0]['streamSettings'].get('xhttpSettings', None) is not None:
            if not is_reality:
                result['outbounds'][0]['streamSettings']['xhttpSettings']['host'] = domain
        if result['outbounds'][0]['streamSettings'].get('grpcSettings', None) is not None:
            if not is_reality:
                result['outbounds'][0]['streamSettings']['grpcSettings']['authority'] = domain
        if result['outbounds'][0]['streamSettings'].get('wsSettings', None) is not None:
            wsSettings: dict[str, Any] = result['outbounds'][0]['streamSettings']['wsSettings']
            wsSettings.setdefault('headers', {})
            wsSettings['host'] = domain
            wsSettings['headers']['Host'] = domain
        if result['outbounds'][0]['streamSettings'].get('httpupgradeSettings', None) is not None:
            httpupgradeSettings: dict[str, object] = result['outbounds'][0]['streamSettings']['httpupgradeSettings']
            httpupgradeSettings['host'] = domain
        meta = result.setdefault('meta', {})
        meta['serverDescription'] = short_desc  # NOTE: this only works if you have a providerid,
                                                # NOTE: but we set it regardless
        obj.append(result)

    return obj
