"""HTTP subscription responses. Intentionally separate from Subscription and WebApi."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

from flask import Response

from util import err

if TYPE_CHECKING:
    from core import Subscription


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
    # late import: this module is loaded while `builders` is still initializing,
    # and the names below are the ones callers patch on the package
    import builders

    if not token:
        return err("Invalid token.", 401)
    username = obj.user_svc.usertotoken(token)
    obj.audit_svc.audit(name='sub_hit', info={"username": username, "lang": lang, "ua": ua, "ip": ip, "force_json": force_json})

    if not username:
        return err("Invalid token.", 401)
    if lang not in ("ru", "en"):
        return err("Language can be either 'ru' or 'en'", 400)

    if builders.isbrowser(ua=ua):
        html = builders.render_template("browser.html", **builders._browser_strings(obj, lang))
        cfg = obj.res.cfg.view()
        prefix = '/' + '/'.join(p for p in cfg['uri'].split('/') if p)
        html = builders.embed_font_faces(html, prefix)
        return Response(html, mimetype="text/html"), 403

    bandwidths = obj.bandwidth_svc.bandwidth(username)

    cfg = obj.res.cfg.view()
    lang_cfg = obj.res.lang_cfg.view()
    user = obj.user_svc.user(username)

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
        builders.build_description(
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
            response=json.dumps(builders.build_json(
                cfg=cfg, user_uuid=user_uuid, lang=lang, fingerprint=str(user['fingerprint'])
            ), ensure_ascii=False),
            mimetype='application/json; charset=utf-8',
            headers=headers
        ), 200

    return Response(builders.build_link_array(
        cfg=cfg, status=status, statusWl=statusWl, lang=lang,
        bandwidths=bandwidths, user_uuid=user_uuid, is_happ=is_happ, need_dummy_link=need_dummy_link,
        fingerprint=str(user['fingerprint']), lang_cfg=lang_cfg
    ), mimetype="text/plain", headers=headers), 200
