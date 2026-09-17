"""
Module with methods to build links/json arrays for clients.
"""
import time
import urllib.parse
import json
import copy
import base64

from typing import Any, Literal
from datetime import datetime, timezone
from collections import deque

from util import fmt_bytes, format
from custom_types import (
    BandwidthInfo, 
)

__all__ = ["build_description", "build_link_array", "build_json"]

def build_description(
    lang_cfg: dict[str, Any],
    name: str,
    lang: str,
    bandwidths: BandwidthInfo, 
    status: bool,
    statusTime: bool,
    ts: int,
    bw_limit: int,
    bw_used: int,
    wl_limit: int,
    wl_used: int,
) -> str:
    descTable: dict[str, str] = lang_cfg['description'][lang]

    if status:
        desc = descTable["main"]

        desc = format(
            desc,
            up=fmt_bytes(int(bandwidths.upload)),
            down=fmt_bytes(int(bandwidths.download))
        )
        
        if ts != 0:
            ts_str = descTable["date"]
            ts_str = format(
                ts_str,
                date=datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m.%y %H:%M (UTC)"),
                days=str((ts - int(time.time())) // 86400)
            )

            desc = format(
                desc,
                slot_time=ts_str
            )
        else:
            desc = format(
                desc,
                slot_time=""
            )
        
        if bw_limit != 0:
            bw_limit_str = descTable["bw"]
            bw_limit_str = format(
                bw_limit_str,
                used=fmt_bytes(bw_used),
                limit=f"{str(bw_limit)}GB"
            )
            desc = format(
                desc,
                slot_bw=bw_limit_str
            )
        else:
            desc = format(
                desc,
                slot_bw=""
            )

        if wl_limit != 0:
            if wl_used > wl_limit * 10**9:
                wl_bw_str = descTable["wl_bw_exceeded"]
            else:
                wl_bw_str = descTable["wl_bw"]
            
            wl_bw_str = format(
                wl_bw_str,
                used=fmt_bytes(wl_used),
                limit=f"{str(wl_limit)}GB"
            )
            desc = format(
                desc,
                slot_wl_bw=wl_bw_str
            )
        else:
            desc = format(
                desc,
                slot_wl_bw=""
            )
                
    else:
        if bw_limit == 0:
            desc = descTable["main"]

            desc = format(
                desc,
                up=fmt_bytes(int(bandwidths.upload)),
                down=fmt_bytes(int(bandwidths.download)),

                # aren't needed here
                slot_bw="",
                slot_wl_bw=""
            )
        else:
            desc = descTable["main_exceeded"]
            desc = format(
                desc,
                up=fmt_bytes(int(bandwidths.upload)),
                down=fmt_bytes(int(bandwidths.download)),
                used=fmt_bytes(bw_used),
                limit=f"{str(bw_limit)}GB"
            )
        
        if not statusTime:
            time_str = descTable["date_expired"]
            time_str = format(
                time_str,
                date=datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m.%y %H:%M (UTC)"),
                days=str(-(ts - int(time.time())) // 86400)
            )

            desc = format(
                desc,
                slot_time=time_str
            )
        else:
            desc = format(
                desc,
                slot_time=""
            )

    final = format(desc, username=name)
    if "{" in final:
        raise ValueError("formatted description contains unformatted placeholders")

    return final

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
        dt = "Bandwidth: " if lang == "en" else "Трафик: "
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
