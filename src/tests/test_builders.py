from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import cast
from core import Subscription
from typing_contracts import AppOverrides, UserRecordOverrides
from unittest.mock import MagicMock

import pytest

from flask import Flask

from builders import build_description, build_json, build_link_array, get_subscription
from custom_types import BandwidthInfo
from config import AppConfig, LangConfig, ProfileConfig
from config.documents import ProfileOutbound, ProfileStream, ProfileTemplate
from helpers import config_mock, language_config, subscription_config


DESC = {
        "en": {
            "bw_label": "usage ",
        "main": "{username} up {up} down {down}{slot_time}{slot_bw}{slot_wl_bw}",
        "main_exceeded": "{username} exceeded {used}/{limit}",
        "date": " until {date} ({days}d)",
        "date_expired": " expired {date} ({days}d ago)",
        "bw": " bw {used}/{limit}",
        "wl_bw": " wl {used}/{limit}",
        "wl_bw_exceeded": " wl exceeded {used}/{limit}",
    }
}


def _bandwidth() -> BandwidthInfo:
    return BandwidthInfo(upload=1_500_000_000, download=500_000_000, total=2_000_000_000)


def _lang(description: dict[str, dict[str, str]] = DESC) -> LangConfig:
    doc = language_config()
    doc['description'] = description
    return doc


def test_description_fills_active_and_disabled_slots() -> None:
    active = build_description(
        _lang(), "Alice", "en", _bandwidth(),
        status=True, statusTime=True, ts=1_700_000_000,
        bw_limit=5, bw_used=1_000_000_000, wl_limit=1, wl_used=2_000_000_000,
    )
    assert active.startswith("Alice")
    assert "1.50 GB" in active
    assert "until 14.11.23" in active
    assert "bw 1.00 GB/5GB" in active
    assert "wl exceeded" in active

    unlimited = build_description(
        _lang(), "Alice", "en", _bandwidth(),
        status=True, statusTime=True, ts=0,
        bw_limit=0, bw_used=0, wl_limit=0, wl_used=0,
    )
    assert unlimited == "Alice up 1.50 GB down 500.00 MB"

    disabled = build_description(
        _lang(), "Alice", "en", _bandwidth(),
        status=False, statusTime=True, ts=1_700_000_000,
        bw_limit=0, bw_used=0, wl_limit=0, wl_used=0,
    )
    assert disabled == "Alice up 1.50 GB down 500.00 MB"

    expired = build_description(
        _lang(), "Alice", "en", _bandwidth(),
        status=False, statusTime=False, ts=1_700_000_000,
        bw_limit=2, bw_used=3_000_000_000, wl_limit=0, wl_used=0,
    )
    assert "exceeded" in expired
    assert "expired" not in expired

    with pytest.raises(ValueError, match="unformatted"):
        build_description(
            _lang({"en": {**DESC["en"], "main": "{username} {missing}"}}),
            "Alice", "en", _bandwidth(),
            status=True, statusTime=True, ts=0,
            bw_limit=0, bw_used=0, wl_limit=0, wl_used=0,
        )


def _link_config() -> AppConfig:
    config = subscription_config()
    config.update({
        "profiles": {
            "fast": {
                "name": ["Fast", "Быстрый"],
                "whitelist": False,
                "masterLink": "vless://UUID@DOMAIN:443?fp=FINGERPRINT&extra=EXTRA#NAME",
                "flag": "⚡",
                "node": "edge",
                "xhttpExtra": {"path": "/x"},
                "json": {"settings": {"vnext": []}, "streamSettings": {}}, "description": ["", ""], "shortProfileDescription": ["", ""],
            },
            "wl": {
                "name": ["WL", "ВЛ"],
                "whitelist": True,
                "masterLink": "vless://UUID@DOMAIN:443?fp=FINGERPRINT#NAME",
                "flag": "🛡",
                "node": "wl-node",
                "xhttpExtra": {},
                "json": {"settings": {"vnext": []}, "streamSettings": {}}, "description": ["", ""], "shortProfileDescription": ["", ""],
            },
        },
        "nodes": {"edge": "edge.example", "wl-node": "wl.example"},
    })
    return config


def test_link_array_filters_profiles_and_encodes_extra() -> None:
    encoded = build_link_array(
        _link_config(), status=True, statusWl=False, lang="ru", is_happ=True,
        user_uuid="user-uuid", bandwidths=_bandwidth(), need_dummy_link=True,
        fingerprint="chrome", lang_cfg=_lang({"ru": DESC["en"], "en": DESC["en"]}),
    )
    lines = base64.b64decode(encoded).decode("utf-8").splitlines()
    assert lines[0].startswith("vless://0@localhost:1")
    assert "user-uuid@edge.example" in lines[1]
    assert "fp=chrome" in lines[1]
    assert "%7B%22path%22%3A%22/x%22%7D" in lines[1]
    assert "⚡Быстрый" in lines[1]
    assert len(lines) == 2

    disabled = build_link_array(
        _link_config(), status=False, statusWl=True, lang="en", is_happ=False,
        user_uuid="user-uuid", bandwidths=_bandwidth(), need_dummy_link=False,
        fingerprint="chrome", lang_cfg=_lang(),
    )
    assert base64.b64decode(disabled) == b""

    config = _link_config()
    config["profiles"]["fast"]["xhttpExtra"] = {}
    encoded = build_link_array(
        config, status=True, statusWl=True, lang="en", is_happ=False,
        user_uuid="uuid", bandwidths=_bandwidth(), need_dummy_link=False,
        fingerprint="edge", lang_cfg=_lang(),
    )
    text = base64.b64decode(encoded).decode("utf-8")
    assert "extra=" not in text
    assert "WL" in text
    assert "🛡" not in text


def _json_template() -> ProfileTemplate:
    return {
        "remarks": "",
        "meta": {},
        "outbounds": [{
            "settings": {"vnext": [{"address": "", "users": [{"id": ""}]}]},
            "streamSettings": {},
        }],
    }


def _profile(stream: ProfileStream) -> ProfileOutbound:
    profile = _json_template()["outbounds"][0]
    profile["streamSettings"] = stream
    return profile


def test_json_profiles_fill_transport_hosts() -> None:
    cfg = subscription_config()
    profiles: dict[str, ProfileConfig] = {
        "tls": {
            "description": ["", ""], "whitelist": False, "xhttpExtra": {}, "masterLink": "",
            "name": ["TLS", "ТЛС"], "flag": "", "node": "edge",
            "shortProfileDescription": ["tls en", "tls ru"],
            "json": _profile({
                "tlsSettings": {"serverName": "", "fingerprint": ""},
                "xhttpSettings": {"host": ""},
                "grpcSettings": {"authority": ""},
                "realitySettings": {"fingerprint": ""},
            }),
        },
        "ws": {
            "description": ["", ""], "whitelist": False, "xhttpExtra": {}, "masterLink": "",
            "name": ["WS", "ВС"], "flag": "", "node": "edge",
            "shortProfileDescription": ["ws en", "ws ru"],
            "json": _profile({"wsSettings": {}, "httpupgradeSettings": {"host": ""}}),
        },
    }
    cfg.update({
        "json_template": _json_template(),
        "profiles": profiles,
        "nodes": {"edge": "edge.example"},
    })
    built = build_json(cfg, "user-uuid", "en", "chrome")
    tls = built[0]["outbounds"]
    assert isinstance(tls, list)
    stream = cast(ProfileOutbound, tls[0])["streamSettings"]
    assert "tlsSettings" in stream
    assert "xhttpSettings" in stream
    assert "realitySettings" in stream
    assert stream["tlsSettings"]["serverName"] == "edge.example"
    assert stream["tlsSettings"]["fingerprint"] == "chrome"
    assert "host" not in stream["xhttpSettings"] or stream["xhttpSettings"]["host"] == ""
    assert stream["realitySettings"]["fingerprint"] == "chrome"
    ws = cast(ProfileTemplate, built[1])["outbounds"][0]["streamSettings"]
    assert "wsSettings" in ws and "headers" in ws["wsSettings"]
    assert "httpupgradeSettings" in ws and "host" in ws["httpupgradeSettings"]
    assert ws["wsSettings"]["headers"]["Host"] == "edge.example"
    assert ws["httpupgradeSettings"]["host"] == "edge.example"
    assert built[0]["meta"] == {"serverDescription": "tls en"}


def _subscription(*, user: UserRecordOverrides | None = None, cfg: AppOverrides | None = None, username: str = "alice") -> MagicMock:
    user_data: UserRecordOverrides = {
        "displayname": "Alice",
        "enabled": 1,
        "enabled_time": 1,
        "enabled_wl": 1,
        "expires_at": 0,
        "bw_limit_gb": 0,
        "bw_used": 0,
        "wl_limit_gb": 0,
        "wl_used": 0,
        "uuid": "user-uuid",
        "fingerprint": "chrome",
    }
    user_data.update(user or {})
    config = subscription_config()
    config.update({
        "uri": "/sub/",
        "sub_name": "VPN",
        "provider_id": "",
        "bypass_packages": ["com.example"],
        "ping_check_url": "https://example.test/204",
        "profiles": {},
    })
    config.update(cfg or {})
    subscription = MagicMock(spec=Subscription,
        user_svc=MagicMock(),
        audit_svc=MagicMock(),
        bandwidth_svc=MagicMock(),
        res=SimpleNamespace(
            cfg=MagicMock(),
            lang_cfg=MagicMock(),
        ),
    )
    subscription.user_svc.usertotoken.return_value = username
    subscription.user_svc.user.return_value = user_data
    subscription.bandwidth_svc.bandwidth.return_value = _bandwidth()
    subscription.res.cfg = config_mock(config)
    language = _lang()
    language['web'] = {"shared": {"en": {"forbidden_title": "No browser"}}}
    subscription.res.lang_cfg = config_mock(language)
    return subscription


def test_subscription_rejects_bad_input_and_browser_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    subscription = _subscription()
    app = Flask(__name__)
    with app.app_context():
        response, status = get_subscription(
            subscription, token="", lang="en", ua="v2ray", ip="1.1.1.1", force_json="",
        )
        assert status == 401

        subscription.user_svc.usertotoken.return_value = ""
        response, status = get_subscription(
            subscription, token="bad", lang="en", ua="v2ray", ip="1.1.1.1", force_json="",
        )
        assert status == 401
        subscription.audit_svc.audit.assert_called()

        subscription = _subscription()
        response, status = get_subscription(
            subscription, token="tok", lang="de", ua="v2ray", ip="1.1.1.1", force_json="",
        )
        assert status == 400

        def is_browser(ua: str) -> bool:
            del ua
            return True

        def render(name: str, **kwargs: object) -> str:
            return f"{name}:{kwargs['forbidden_title']}"

        def embed(html: str, prefix: str) -> str:
            return f"{prefix}|{html}"

        monkeypatch.setattr("builders.isbrowser", is_browser)
        monkeypatch.setattr(
            "builders.render_template",
            render,
        )
        monkeypatch.setattr("builders.embed_font_faces", embed)
        response, status = get_subscription(
            subscription, token="tok", lang="en", ua="Mozilla", ip="1.1.1.1", force_json="",
        )
        assert status == 403
        assert response.get_data(as_text=True) == "/sub|browser.html:No browser"


def test_subscription_returns_links_or_happ_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def not_browser(ua: str) -> bool:
        del ua
        return False

    def description(**kwargs: object) -> str:
        del kwargs
        return "desc"

    def links(**kwargs: object) -> str:
        del kwargs
        return "links"

    def happ_json(**kwargs: object) -> list[dict[str, object]]:
        return [{"remarks": kwargs["lang"]}]

    monkeypatch.setattr("builders.isbrowser", not_browser)
    monkeypatch.setattr("builders.build_description", description)
    monkeypatch.setattr("builders.build_link_array", links)
    monkeypatch.setattr("builders.build_json", happ_json)

    subscription = _subscription(user={"bw_limit_gb": 2, "bw_used": 1_000_000_000, "enabled": 0})
    response, status = get_subscription(
        subscription, token="tok", lang="en", ua="v2rayN", ip="1.1.1.1", force_json="",
    )
    assert status == 200
    assert response.get_data(as_text=True) == "links"
    assert response.headers["Profile-Title"] == "VPN"
    assert "total=" in response.headers["Subscription-Userinfo"]

    subscription = _subscription(cfg={
        "provider_id": "provider",
        "fallback_domain": "https://fallback.test/",
        "uri": "sub",
        "sub_name": "VPN",
        "bypass_packages": ["a", "b"],
    })
    response, status = get_subscription(
        subscription, token="tok", lang="ru", ua="Happ/1.0", ip="1.1.1.1", force_json="1",
    )
    assert status == 200
    assert response.mimetype == "application/json"
    assert json.loads(response.get_data(as_text=True)) == [{"remarks": "ru"}]
    assert response.headers["providerid"] == "provider"
    assert "force_json=1" in response.headers["fallback-url"]
    assert response.headers["per-app-proxy-list"] == "a,b"
