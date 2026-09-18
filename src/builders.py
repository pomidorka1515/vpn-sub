"""
Module with methods to build links/json arrays for clients.
"""
import time
import urllib.parse
import json
import copy
import base64

from typing import Any, Literal, TYPE_CHECKING
from datetime import datetime, timezone
from collections import deque
from flask import Response

from util import fmt_bytes, format, isbrowser, err
from custom_types import (
    BandwidthInfo, 
)

if TYPE_CHECKING:
    from core import Subscription

__all__ = ["build_description", "build_link_array", "build_json", "get_subscription"]

# pyright: reportPrivateUsage=false

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

def get_subscription(
    obj: Subscription,
    /,
    *,
    token: str,
    lang: str,
    ua: str,
    ip: str,
    force_json: str
) -> tuple[Response, int]:
    # ===================================
    # intentionally separate from `Subscription`/`WebApi`:
    # this doesnt fit in either class (`WebApi` is an optional component, subscription generation is not)
    # P.S. this still heavily relies on the `Subscription` instance, use `obj` instead of `self`
    # ===================================
    if not token:
        return err("Invalid token.", 401)
    username = obj.usertotoken(token)
    obj.audit(name='sub_hit', info={"username": username, "lang": lang, "ua": ua, "ip": ip, "force_json": force_json})

    if not username:
        return err("Invalid token.", 401)
    if lang not in ("ru", "en"):
        return err("Language can be either 'ru' or 'en'", 400)

    if isbrowser(ua=ua):
        return Response(obj.browser_html, mimetype="text/html"), 403

    bandwidths = obj.bandwidth(username)

    cfg = obj.cfg.copy()
    lang_cfg = obj.lang_cfg.copy()
    user = obj._user(username)

    displayname = str(user['displayname'])
    need_dummy_link = "v2rayn" in ua.lower() # catches both v2rayn and v2rayng
    is_happ = ua.startswith("Happ/")
    uri: str = cfg['uri'].strip("/")

    status = bool(user['enabled'])
    statusTime = bool(user['enabled_time'])
    statusWl = bool(user['enabled_wl'])
    times = int(user['expires_at'])
    sub_name: str = cfg['sub_name']

    desc = f"base64:{base64.b64encode(
        build_description(
            lang_cfg=lang_cfg,
            name=displayname,
            lang=lang,
            bandwidths=bandwidths,
            status=status,
            statusTime=statusTime,
            ts=times,
            bw_limit=int(user['bw_limit_gb']),
            bw_used=int(user['bw_used']),
            wl_limit=int(user['wl_limit_gb']),
            wl_used=int(user['wl_used']),
        ).encode('utf-8')).decode('utf-8')}"

    SCALE = (1 << 30) / 1e9 
    
    total = int(user['bw_limit_gb']) << 30 # convert to GiB directly
    if status and not total:
        upload, download = (int(b) * SCALE for b in bandwidths[:2])
    else:
        # monthly quota is intentionally not separated into upload/download, the split wouldn't be visible either way
        upload = download = (int(user['bw_used']) * SCALE if status else total) / 2

    userinfo = f"upload={upload:.0f};download={download:.0f};total={total:.0f};expire={times:.0f}"

    headers: dict[str, str] = {
        'Profile-Title': sub_name,
        'Subscription-Userinfo': userinfo,
        'profile-update-interval': "1",
        'X-If-Youre-Reading-This': 'Your-Subscription-Has-Been-Revoked',
        'announce': desc,
        'Content-Type': "text/plain"
    }

    provider_id = cfg['provider_id']

    if provider_id and is_happ:
        fallback_domain: str | None = cfg.get('fallback_domain', None)
        provider_id_headers: dict[str, str] = {
            'providerid': provider_id,
            'per-app-proxy-mode': 'bypass',
            'per-app-proxy-list': ','.join(cfg['bypass_packages']),
            'no-limit-xhttp-enabled': '1',
            'check-url-via-proxy': cfg.get('ping_check_url', 'https://google.com/generate_204'),
            'ping-type': 'proxy',
            'sniffing-enable': '1', # Routing works better
            'ping-result': 'time',
            'dont-use-filter': '1',
            'manual-block-user-agent': '1',
            'subscriptions-sort-type': 'without',
            'proxy-ping-timeout': '5' # NOTE: iOS only for some reason
        }
        if fallback_domain is not None:
            provider_id_headers['fallback-url'] = \
                f'{fallback_domain.strip('/')}/{uri.strip('/')}?token={token}&lang={lang}{"&force_json=" + force_json if force_json else ""}'

        headers.update(provider_id_headers)

    user_uuid = str(user['uuid'])

    if is_happ or force_json == '1':
        headers['Content-Type'] = 'application/json'
        return Response(
            response=json.dumps(build_json(
                cfg=cfg, user_uuid=user_uuid, lang=lang, fingerprint=str(user['fingerprint'])
            ), ensure_ascii=False),
            mimetype='application/json; charset=utf-8',
            headers=headers
        ), 200

    else:
        return Response(build_link_array(
            cfg=cfg, status=status, statusWl=statusWl, lang=lang,
            bandwidths=bandwidths, user_uuid=user_uuid, is_happ=is_happ, need_dummy_link=need_dummy_link,
            fingerprint=str(user['fingerprint'])
        ), mimetype="text/plain", headers=headers), 200
