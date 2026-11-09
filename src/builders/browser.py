"""Browser landing-page strings for subscription URLs opened outside a client."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from core import Subscription


def _string_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: text
        for key, text in cast(Mapping[object, object], value).items()
        if isinstance(key, str) and isinstance(text, str)
    }


def _section(web: Mapping[str, object], name: str) -> Mapping[str, object]:
    section = web.get(name)
    if isinstance(section, Mapping):
        return cast(Mapping[str, object], section)
    return {}


def _browser_strings(
    obj: Subscription, lang: str, *, token_required: bool = False, dashboard: bool = False
) -> dict[str, str]:
    language = obj.res.lang_cfg.view()
    raw = cast(object, language.get("web"))
    web: Mapping[str, object] = cast(Mapping[str, object], raw) if isinstance(raw, Mapping) else {}
    langs = _section(web, "shared")
    strings = _string_map(langs.get(lang)) or _string_map(langs.get("en"))

    def text(key: str, fallback: str) -> str:
        return strings.get(key, fallback)

    common = {
        "back_label": text("back_to_dashboard", "Back to dashboard")
        if dashboard
        else text("back_to_login", "Back to login"),
    }
    if token_required:
        return common | {
            "forbidden_title": text("forbidden_title", "403 Forbidden"),
            "use_vpn_client": text("token_required", "Valid token required."),
            "use_vpn_client_hint": text(
                "token_required_hint",
                "A valid token is required to retrieve the subscription.",
            ),
        }

    return common | {
        "forbidden_title": text("forbidden_title", "403 Forbidden"),
        "use_vpn_client": text("use_vpn_client", "Use a VPN client."),
        "use_vpn_client_hint": text(
            "use_vpn_client_hint",
            "Use a VPN client to get the subscription.",
        ),
    }
