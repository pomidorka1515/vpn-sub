from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from bots.public.subscription import PublicSubscriptionMixin
from custom_types import UserInfo, UserInfoBandwidth, UserInfoBandwidthTotal


INFO_TEXTS = {
    "en": {
        "info_text": (
            "ℹ️ <b>Your Profile:</b> <code>{username}</code>\n"
            "Enabled: {status}\n"
            "Access to WL locations: {wl_status}\n"
            "Online: {online}\n\n"
            "📊 <b>Traffic:</b>\n"
            "Total downloaded: {total}\n"
            "This month: {monthly}\n"
            "WL traffic total: {wl_total}\n"
            "WL traffic this month: {wl_monthly}\n"
            "Detailed info:\n"
            "↑ {up} / ↓ {down}\n"
            "WL: ↑ {wl_up} / ↓ {wl_down}\n\n"
            "<i>WL = whitelist locations</i>\n\n"
            "⏳ <b>Time remaining:</b> {days}\n"
            "Fingerprint: <code>{fingerprint}</code>"
        ),
        "unlimited": "Unlimited",
        "lifetime": "Lifetime",
    },
    "ru": {
        "info_text": (
            "ℹ️ <b>Ваш профиль:</b> <code>{username}</code>\n"
            "Включен: {status}\n"
            "Доступ к WL-локациям: {wl_status}\n"
            "В сети: {online}\n\n"
            "📊 <b>Трафик:</b>\n"
            "Скачано за всё время: {total}\n"
            "В этом месяце: {monthly}\n"
            "WL за всё время: {wl_total}\n"
            "WL в этом месяце: {wl_monthly}\n"
            "Подробная информация:\n"
            "↑ {up} / ↓ {down}\n"
            "WL: ↑ {wl_up} / ↓ {wl_down}\n\n"
            "<i>WL = whitelist-локации</i>\n\n"
            "⏳ <b>Осталось времени:</b> {days}\n"
            "Отпечаток: <code>{fingerprint}</code>"
        ),
        "unlimited": "Безлимит",
        "lifetime": "Навсегда",
    },
}


@pytest.fixture
def info_mixin() -> tuple[PublicSubscriptionMixin, MagicMock, MagicMock]:
    mixin = PublicSubscriptionMixin.__new__(PublicSubscriptionMixin)
    telegram = MagicMock()
    subscription = MagicMock()
    mixin.bot = cast(Any, telegram)
    mixin.sub = cast(Any, subscription)
    mixin.TEXTS = INFO_TEXTS
    mixin.get_menu = MagicMock(return_value="menu")  # type: ignore[method-assign]
    return mixin, telegram, subscription


def _info(**overrides: Any) -> UserInfo:
    bandwidth = UserInfoBandwidth(
        total=UserInfoBandwidthTotal(
            upload=13_785_700_000,
            download=113_972_680_000,
            total=127_758_370_000,
        ),
        wl_total=UserInfoBandwidthTotal(upload=0, download=0, total=0),
        monthly=0,
        wl_monthly=0,
        limit=0,
        wl_limit=5,
    )
    payload: dict[str, Any] = {
        "_": "test",
        "token": "t" * 40,
        "link": "https://example.test/sub?token=t",
        "displayname": "PomiDor",
        "uuid": "01234567-89ab-cdef-0123-456789abcdef",
        "fingerprint": "edge",
        "enabled": True,
        "wl_enabled": True,
        "time": 0,
        "online": True,
        "bandwidth": bandwidth,
    }
    payload.update(overrides)
    return UserInfo(**payload)


def test_english_info_uses_auto_units_and_html(
    info_mixin: tuple[PublicSubscriptionMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = info_mixin
    subscription.telegram_svc.get_info_telegram.return_value = _info()
    mixin.send_info(7, 42, "en")
    telegram.send_message.assert_called_once()
    args, kwargs = telegram.send_message.call_args
    text = args[1]
    assert kwargs["parse_mode"] == "HTML"
    assert "Total downloaded: 127.76 GB" in text
    assert "This month: <i>Unlimited</i>" in text
    assert "WL traffic total: 0 B" in text
    assert "WL traffic this month: 0 B / 5 GB" in text
    assert "↑ 13.79 GB / ↓ 113.97 GB" in text
    assert "WL: ↑ 0 B / ↓ 0 B" in text
    assert "<i>WL = whitelist locations</i>" in text
    assert "⏳ <b>Time remaining:</b> Lifetime" in text
    assert "Fingerprint: <code>edge</code>" in text
    assert "Unlimited MB" not in text


def test_russian_info_matches_english_layout(
    info_mixin: tuple[PublicSubscriptionMixin, MagicMock, MagicMock],
) -> None:
    mixin, telegram, subscription = info_mixin
    subscription.telegram_svc.get_info_telegram.return_value = _info()
    mixin.send_info(7, 42, "ru")
    text = telegram.send_message.call_args.args[1]
    assert "Скачано за всё время: 127.76 GB" in text
    assert "В этом месяце: <i>Безлимит</i>" in text
    assert "WL за всё время: 0 B" in text
    assert "WL в этом месяце: 0 B / 5 GB" in text
    assert "Отпечаток: <code>edge</code>" in text
    assert "⏳ <b>Осталось времени:</b> Навсегда" in text
