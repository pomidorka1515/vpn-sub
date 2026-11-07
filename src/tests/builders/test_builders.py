from __future__ import annotations

import base64
import json
import threading
import urllib.parse
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from app_runtime.offload import OffloadServices
from builders import build_description, build_json, build_link_array, get_subscription
from config.documents import ProfileOutbound, ProfileTemplate
from core import Subscription
from custom_types import BandwidthInfo
from tests.support.config import config_mock, language_config, subscription_config

if TYPE_CHECKING:
    from config import AppConfig, LangConfig, ProfileConfig
    from config.documents import ProfileStream
    from tests.support.contracts import AppOverrides, UserRecordOverrides

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
    },
}


def _bandwidth() -> BandwidthInfo:
    return BandwidthInfo(upload=1_500_000_000, download=500_000_000, total=2_000_000_000)


def _lang(description: dict[str, dict[str, str]] = DESC) -> LangConfig:
    doc = language_config()
    doc["description"] = description
    return doc


def test_description_fills_active_and_disabled_slots() -> None:
    active = build_description(
        _lang(),
        "Alice",
        "en",
        _bandwidth(),
        status=True,
        statusTime=True,
        ts=1_700_000_000,
        bw_limit=5,
        bw_used=1_000_000_000,
        wl_limit=1,
        wl_used=2_000_000_000,
    )
    assert active.startswith("Alice")
    assert "1.50 GB" in active
    assert "until 14.11.23" in active
    assert "bw 1.00 GB/5GB" in active
    assert "wl exceeded" in active

    unlimited = build_description(
        _lang(),
        "Alice",
        "en",
        _bandwidth(),
        status=True,
        statusTime=True,
        ts=0,
        bw_limit=0,
        bw_used=0,
        wl_limit=0,
        wl_used=0,
    )
    assert unlimited == "Alice up 1.50 GB down 500.00 MB"

    disabled = build_description(
        _lang(),
        "Alice",
        "en",
        _bandwidth(),
        status=False,
        statusTime=True,
        ts=1_700_000_000,
        bw_limit=0,
        bw_used=0,
        wl_limit=0,
        wl_used=0,
    )
    assert disabled == "Alice up 1.50 GB down 500.00 MB"

    expired = build_description(
        _lang(),
        "Alice",
        "en",
        _bandwidth(),
        status=False,
        statusTime=False,
        ts=1_700_000_000,
        bw_limit=2,
        bw_used=3_000_000_000,
        wl_limit=0,
        wl_used=0,
    )
    assert "exceeded" in expired
    assert "expired" not in expired

    assert (
        build_description(
            _lang({"en": {**DESC["en"], "main": "{username} {missing}"}}),
            "Alice",
            "en",
            _bandwidth(),
            status=True,
            statusTime=True,
            ts=0,
            bw_limit=0,
            bw_used=0,
            wl_limit=0,
            wl_used=0,
        )
        == "Alice {missing}"
    )


@pytest.mark.parametrize(
    ("used", "expected_whitelist"),
    [
        (999_999_999, " wl 1000.00 MB/1GB"),
        (1_000_000_000, " wl exceeded 1.00 GB/1GB"),
        (1_000_000_001, " wl exceeded 1.00 GB/1GB"),
    ],
    ids=["just-below-quota", "at-quota", "just-above-quota"],
)
def test_description_whitelist_quota_boundary(used: int, expected_whitelist: str) -> None:
    description = build_description(
        _lang(),
        "Alice",
        "en",
        _bandwidth(),
        status=True,
        statusTime=True,
        ts=0,
        bw_limit=0,
        bw_used=0,
        wl_limit=1,
        wl_used=used,
    )

    assert description == f"Alice up 1.50 GB down 500.00 MB{expected_whitelist}"


@pytest.mark.parametrize("time_enabled", [True, False], ids=["disabled", "expired"])
@pytest.mark.parametrize(
    ("limit", "used", "exhausted"),
    [
        (0, 3_000_000_000, False),
        (2, 0, False),
        (2, 1_000_000_000, False),
        (2, 1_999_999_999, False),
        (2, 2_000_000_000, True),
        (2, 2_000_000_001, True),
    ],
    ids=["unlimited", "unused", "below-quota", "just-below-quota", "at-quota", "above-quota"],
)
def test_disabled_description_reports_only_exhausted_quota(
    monkeypatch: pytest.MonkeyPatch,
    time_enabled: bool,
    limit: int,
    used: int,
    exhausted: bool,
) -> None:
    monkeypatch.setattr("builders.description.time", SimpleNamespace(time=lambda: 1_700_086_400))
    language = _lang(
        {
            "en": {
                **DESC["en"],
                "main_exceeded": "{username} exceeded {used}/{limit} up {up} down {down}{slot_time}",
            },
        },
    )
    description = build_description(
        language,
        "Alice",
        "en",
        _bandwidth(),
        status=False,
        statusTime=time_enabled,
        ts=1_700_000_000,
        bw_limit=limit,
        bw_used=used,
        wl_limit=1,
        wl_used=500_000_000,
    )

    if exhausted:
        assert description.startswith("Alice exceeded 2.00 GB/2GB")
    else:
        assert description.startswith("Alice up")
        assert "exceeded" not in description
    assert "up 1.50 GB down 500.00 MB" in description
    if time_enabled:
        assert "expired" not in description
    else:
        assert description.endswith(" expired 14.11.23 22:13 (UTC) (1d ago)")
    assert "{" not in description
    assert "}" not in description


def _link_config() -> AppConfig:
    config = subscription_config()
    config.update(
        {
            "profiles": {
                "fast": {
                    "name": ["Fast", "Быстрый"],
                    "whitelist": False,
                    "masterLink": "vless://UUID@DOMAIN:443?fp=FINGERPRINT&extra=EXTRA#NAME",
                    "flag": "⚡",
                    "node": "edge",
                    "xhttpExtra": {"path": "/x"},
                    "json": {"settings": {"vnext": []}, "streamSettings": {}},
                    "description": ["", ""],
                    "shortProfileDescription": ["", ""],
                },
                "wl": {
                    "name": ["WL", "ВЛ"],
                    "whitelist": True,
                    "masterLink": "vless://UUID@DOMAIN:443?fp=FINGERPRINT#NAME",
                    "flag": "🛡",
                    "node": "wl-node",
                    "xhttpExtra": {},
                    "json": {"settings": {"vnext": []}, "streamSettings": {}},
                    "description": ["", ""],
                    "shortProfileDescription": ["", ""],
                },
            },
            "nodes": {"edge": "edge.example", "wl-node": "wl.example"},
        },
    )
    return config


def test_link_array_filters_profiles_and_encodes_extra() -> None:
    encoded = build_link_array(
        _link_config(),
        status=True,
        statusWl=False,
        lang="ru",
        is_happ=True,
        user_uuid="user-uuid",
        bandwidths=_bandwidth(),
        need_dummy_link=True,
        fingerprint="chrome",
        lang_cfg=_lang({"ru": DESC["en"], "en": DESC["en"]}),
    )
    lines = base64.b64decode(encoded).decode("utf-8").splitlines()
    assert lines[0].startswith("vless://0@localhost:1")
    assert "user-uuid@edge.example" in lines[1]
    assert "fp=chrome" in lines[1]
    assert "%7B%22path%22%3A%22/x%22%7D" in lines[1]
    assert urllib.parse.unquote(lines[1].split("#", 1)[1]) == "⚡Быстрый"
    assert len(lines) == 2

    disabled = build_link_array(
        _link_config(),
        status=False,
        statusWl=True,
        lang="en",
        is_happ=False,
        user_uuid="user-uuid",
        bandwidths=_bandwidth(),
        need_dummy_link=False,
        fingerprint="chrome",
        lang_cfg=_lang(),
    )
    assert base64.b64decode(disabled) == b""

    config = _link_config()
    config["profiles"]["fast"]["xhttpExtra"] = {}
    encoded = build_link_array(
        config,
        status=True,
        statusWl=True,
        lang="en",
        is_happ=False,
        user_uuid="uuid",
        bandwidths=_bandwidth(),
        need_dummy_link=False,
        fingerprint="edge",
        lang_cfg=_lang(),
    )
    text = base64.b64decode(encoded).decode("utf-8")
    assert "extra=" not in text
    assert "WL" in text
    assert "🛡" not in text


@pytest.mark.parametrize("is_happ", [False, True], ids=["plain", "happ-flag"])
@pytest.mark.parametrize("lang", ["en", "ru"])
@pytest.mark.parametrize(
    ("name", "escaped"),
    [
        ("Two words\t", "Two%20words%09"),
        ("First\r\nSecond", "First%0D%0ASecond"),
        ("100%20", "100%2520"),
        ("Быстрый", "%D0%91%D1%8B%D1%81%D1%82%D1%80%D1%8B%D0%B9"),
        ("path/name?#", "path%2Fname%3F%23"),
    ],
    ids=["whitespace", "newlines", "percent", "non-ascii", "reserved"],
)
def test_link_array_encodes_profile_fragment(
    name: str,
    escaped: str,
    lang: str,
    is_happ: bool,
) -> None:
    config = _link_config()
    profile = config["profiles"]["fast"]
    profile["name"] = [name, "unused"] if lang == "en" else ["unused", name]
    profile["flag"] = "🇩🇪 \n%/"
    encoded = build_link_array(
        config,
        status=True,
        statusWl=False,
        lang=lang,
        is_happ=is_happ,
        user_uuid="user-uuid",
        bandwidths=_bandwidth(),
        need_dummy_link=False,
        fingerprint="chrome",
        lang_cfg=_lang(),
    )

    lines = base64.b64decode(encoded).decode("utf-8").splitlines()
    assert len(lines) == 1
    fragment = urllib.parse.urlsplit(lines[0]).fragment
    escaped_flag = "%F0%9F%87%A9%F0%9F%87%AA%20%0A%25%2F" if is_happ else ""
    assert fragment == escaped_flag + escaped
    assert urllib.parse.unquote(fragment) == (profile["flag"] if is_happ else "") + name


def _json_template() -> ProfileTemplate:
    return {
        "remarks": "",
        "meta": {},
        "outbounds": [
            {
                "settings": {"vnext": [{"address": "", "users": [{"id": ""}]}]},
                "streamSettings": {},
            },
        ],
    }


def _profile(stream: ProfileStream) -> ProfileOutbound:
    profile = _json_template()["outbounds"][0]
    profile["streamSettings"] = stream
    return profile


def test_json_profiles_fill_transport_hosts() -> None:
    cfg = subscription_config()
    profiles: dict[str, ProfileConfig] = {
        "tls": {
            "description": ["", ""],
            "whitelist": False,
            "xhttpExtra": {},
            "masterLink": "",
            "name": ["TLS", "ТЛС"],
            "flag": "",
            "node": "edge",
            "shortProfileDescription": ["tls en", "tls ru"],
            "json": _profile(
                {
                    "tlsSettings": {"serverName": "", "fingerprint": ""},
                    "xhttpSettings": {"host": ""},
                    "grpcSettings": {"authority": ""},
                    "realitySettings": {"fingerprint": ""},
                },
            ),
        },
        "ws": {
            "description": ["", ""],
            "whitelist": False,
            "xhttpExtra": {},
            "masterLink": "",
            "name": ["WS", "ВС"],
            "flag": "",
            "node": "edge",
            "shortProfileDescription": ["ws en", "ws ru"],
            "json": _profile({"wsSettings": {}, "httpupgradeSettings": {"host": ""}}),
        },
    }
    cfg.update(
        {
            "json_template": _json_template(),
            "profiles": profiles,
            "nodes": {"edge": "edge.example"},
        },
    )
    built = build_json(cfg, "user-uuid", "en", "chrome", status=True, statusWl=True)
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
    assert "wsSettings" in ws
    assert "headers" in ws["wsSettings"]
    assert "httpupgradeSettings" in ws
    assert "host" in ws["httpupgradeSettings"]
    assert ws["wsSettings"]["headers"]["Host"] == "edge.example"
    assert ws["httpupgradeSettings"]["host"] == "edge.example"
    assert built[0]["meta"] == {"serverDescription": "tls en"}


def _subscription(
    *,
    user: UserRecordOverrides | None = None,
    cfg: AppOverrides | None = None,
    username: str = "alice",
) -> MagicMock:
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
    config.update(
        {
            "uri": "/sub/",
            "sub_name": "VPN",
            "provider_id": "",
            "bypass_packages": ["com.example"],
            "ping_check_url": "https://example.test/204",
            "profiles": {},
        },
    )
    config.update(cfg or {})
    subscription = MagicMock(
        spec=Subscription,
        user_svc=AsyncMock(),
        audit_svc=AsyncMock(),
        bandwidth_svc=AsyncMock(),
        res=SimpleNamespace(
            cfg=MagicMock(),
            lang_cfg=MagicMock(),
            offloads=OffloadServices(),
        ),
    )
    subscription.user_svc.usertotoken.return_value = username
    subscription.user_svc.user.return_value = user_data
    subscription.bandwidth_svc.bandwidth.return_value = _bandwidth()
    subscription.res.cfg = config_mock(config)
    language = _lang()
    language["web"] = {"shared": {"en": {"forbidden_title": "No browser"}}}
    subscription.res.lang_cfg = config_mock(language)
    return subscription


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ua", "force_json"),
    [("Happ/1.0", ""), ("v2ray", "1")],
    ids=["happ", "force-json"],
)
@pytest.mark.parametrize(
    ("enabled", "enabled_wl", "expected_names"),
    [
        (0, 1, []),
        (0, 0, []),
        (1, 0, ["Fast"]),
        (1, 1, ["Fast", "WL"]),
    ],
    ids=["main-disabled", "both-disabled", "whitelist-disabled", "both-enabled"],
)
async def test_subscription_json_respects_profile_eligibility(
    ua: str,
    force_json: str,
    enabled: int,
    enabled_wl: int,
    expected_names: list[str],
) -> None:
    cfg = _link_config()
    cfg["json_template"] = _json_template()
    for profile in cfg["profiles"].values():
        profile["json"] = _profile({"tlsSettings": {"serverName": "", "fingerprint": ""}})
        profile["flag"] = ""
    subscription = _subscription(
        user={"enabled": enabled, "enabled_wl": enabled_wl},
        cfg={
            "profiles": cfg["profiles"],
            "nodes": cfg["nodes"],
            "json_template": cfg["json_template"],
        },
    )

    response, status = await get_subscription(
        subscription,
        token="tok",
        lang="en",
        ua=ua,
        ip="192.0.2.1",
        force_json=force_json,
    )
    assert status == 200
    assert response.mimetype == "application/json"
    assert response.headers["Cache-Control"] == "private, no-store"
    profiles = cast(list[ProfileTemplate], json.loads(await response.get_data(as_text=True)))
    assert [profile["remarks"] for profile in profiles] == expected_names
    for built_profile in profiles:
        outbound = built_profile["outbounds"][0]
        assert outbound["settings"]["vnext"][0]["users"][0]["id"] == "user-uuid"
        assert outbound["settings"]["vnext"][0]["address"] in {"edge.example", "wl.example"}

    link_response, link_status = await get_subscription(
        subscription,
        token="tok",
        lang="en",
        ua="v2ray",
        ip="192.0.2.1",
        force_json="",
    )
    assert link_status == 200
    assert link_response.headers["Cache-Control"] == "private, no-store"
    links = base64.b64decode(await link_response.get_data()).decode("utf-8").splitlines()
    assert [link.rsplit("#", 1)[1] for link in links] == expected_names


@pytest.mark.asyncio
async def test_subscription_rejects_bad_input_and_browser_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subscription = _subscription()
    app = Quart(__name__)
    async with app.app_context():
        response, status = await get_subscription(
            subscription,
            token="",
            lang="en",
            ua="v2ray",
            ip="1.1.1.1",
            force_json="",
        )
        assert status == 401
        assert response.headers["Cache-Control"] == "private, no-store"

        subscription.user_svc.usertotoken.return_value = ""
        response, status = await get_subscription(
            subscription,
            token="bad",
            lang="en",
            ua="v2ray",
            ip="1.1.1.1",
            force_json="",
        )
        assert status == 401
        assert response.headers["Cache-Control"] == "private, no-store"
        subscription.audit_svc.audit.assert_called()

        subscription = _subscription()
        response, status = await get_subscription(
            subscription,
            token="tok",
            lang="de",
            ua="v2ray",
            ip="1.1.1.1",
            force_json="",
        )
        assert status == 400
        assert response.headers["Cache-Control"] == "private, no-store"

        def is_browser(ua: str) -> bool:
            del ua
            return True

        async def render(name: str, **kwargs: object) -> str:
            return f"{name}:{kwargs['forbidden_title']}"

        event_loop_thread = threading.get_ident()

        def embed(html: str, prefix: str) -> str:
            assert threading.get_ident() != event_loop_thread
            return f"{prefix}|{html}"

        monkeypatch.setattr("builders.isbrowser", is_browser)
        monkeypatch.setattr(
            "builders.render_template",
            render,
        )
        monkeypatch.setattr("builders.embed_font_faces", embed)
        response, status = await get_subscription(
            subscription,
            token="tok",
            lang="en",
            ua="Mozilla",
            ip="1.1.1.1",
            force_json="",
        )
        assert status == 403
        assert response.mimetype == "text/html"
        assert response.headers["Cache-Control"] == "private, no-store"
        assert await response.get_data(as_text=True) == "/sub|browser.html:No browser"


@pytest.mark.asyncio
async def test_subscription_returns_links_or_happ_json(monkeypatch: pytest.MonkeyPatch) -> None:
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
    response, status = await get_subscription(
        subscription,
        token="tok",
        lang="en",
        ua="v2rayN",
        ip="1.1.1.1",
        force_json="",
    )
    assert status == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert await response.get_data(as_text=True) == "links"
    assert response.headers["Profile-Title"] == "VPN"
    assert "total=" in response.headers["Subscription-Userinfo"]

    subscription = _subscription(
        cfg={
            "provider_id": "provider",
            "fallback_domain": "https://fallback.test/",
            "uri": "sub",
            "sub_name": "VPN",
            "bypass_packages": ["a", "b"],
        },
    )
    response, status = await get_subscription(
        subscription,
        token="tok",
        lang="ru",
        ua="Happ/1.0",
        ip="1.1.1.1",
        force_json="1",
    )
    assert status == 200
    assert response.mimetype == "application/json"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert json.loads(await response.get_data(as_text=True)) == [{"remarks": "ru"}]
    assert response.headers["providerid"] == "provider"
    assert "force_json=1" in response.headers["fallback-url"]
    assert response.headers["per-app-proxy-list"] == "a,b"
