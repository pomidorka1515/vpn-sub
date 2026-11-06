from __future__ import annotations

import re
from html.parser import HTMLParser
from unittest.mock import MagicMock, patch

import pytest

from bots.telegram.admin.leaderboard import AdminLeaderboardMixin
from tests.support.telegram import subscription_mock, telegram_mock


class CaptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_bold = False
        self.bold_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        assert tag == "b"
        assert not attrs
        assert not self.in_bold
        self.in_bold = True

    def handle_endtag(self, tag: str) -> None:
        assert tag == "b"
        assert self.in_bold
        self.in_bold = False

    def handle_data(self, data: str) -> None:
        if self.in_bold:
            self.bold_parts.append(data)


@pytest.fixture
def leaderboard() -> tuple[AdminLeaderboardMixin, MagicMock, MagicMock]:
    mixin = AdminLeaderboardMixin.__new__(AdminLeaderboardMixin)
    telegram = telegram_mock()
    subscription = subscription_mock()
    mixin.bot = telegram
    mixin.sub = subscription
    lang_cfg = MagicMock()
    lang_cfg.view.return_value = {"chart": {"ru": {}}}
    mixin.lang_cfg = lang_cfg
    return mixin, telegram, subscription


async def _caption(
    leaderboard: tuple[AdminLeaderboardMixin, MagicMock, MagicMock], data: dict[str, int]
) -> str:
    mixin, telegram, subscription = leaderboard
    subscription.leaderboard_svc.leaderboard.return_value = data
    with patch("bots.telegram.admin.leaderboard.leaderboard_chart", return_value=b"png"):
        await mixin._handle_leaderboard_result(7, "total", "desc", 0)
    telegram.send_photo.assert_awaited_once()
    assert telegram.send_photo.await_args.kwargs["parse_mode"] == "HTML"
    return str(telegram.send_photo.await_args.args[2])


def _parse_caption(caption: str) -> CaptionParser:
    assert len(caption.encode("utf-8")) <= 1024
    assert len(caption.encode("utf-16-le")) // 2 <= 1024
    assert re.search(r"&(?!amp;|lt;|gt;)", caption) is None
    parser = CaptionParser()
    parser.feed(caption)
    parser.close()
    assert not parser.in_bold
    return parser


@pytest.mark.asyncio
async def test_leaderboard_escapes_legacy_labels(
    leaderboard: tuple[AdminLeaderboardMixin, MagicMock, MagicMock],
) -> None:
    label = 'Alice <b> & "Bob" &#123;'
    caption = await _caption(leaderboard, {label: 2, "ordinary": 1})

    parser = _parse_caption(caption)
    assert parser.bold_parts == ["Список лидеров по трафику:", label, "ordinary"]
    assert "&lt;b&gt;" in caption
    assert "&amp;#123;" in caption
    assert "Пропущено" not in caption


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["a" * 2000, "😀" * 600, "<&ю" * 500])
async def test_leaderboard_omits_oversized_first_row_whole(
    leaderboard: tuple[AdminLeaderboardMixin, MagicMock, MagicMock], label: str
) -> None:
    caption = await _caption(leaderboard, {label: 2, "second": 1})

    parser = _parse_caption(caption)
    assert parser.bold_parts == ["Список лидеров по трафику:"]
    assert "... Пропущено записей: 2" in caption
    assert "second" not in caption


@pytest.mark.asyncio
async def test_leaderboard_keeps_complete_rows_and_reports_remainder(
    leaderboard: tuple[AdminLeaderboardMixin, MagicMock, MagicMock],
) -> None:
    data = {f"user {i} <&> 😀 {'x' * 50}": 100 - i for i in range(20)}
    caption = await _caption(leaderboard, data)

    parser = _parse_caption(caption)
    shown = parser.bold_parts[1:]
    assert shown == list(data)[: len(shown)]
    assert 0 < len(shown) < len(data)
    assert f"... Пропущено записей: {len(data) - len(shown)}" in caption


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [0, 1])
async def test_leaderboard_exact_serialized_byte_boundary(
    leaderboard: tuple[AdminLeaderboardMixin, MagicMock, MagicMock], extra: int
) -> None:
    empty_caption = "<b>Список лидеров по трафику:</b>\n\n🥇 <b></b> - 1 B"
    label = "x" * (1024 - len(empty_caption.encode("utf-8")) + extra)
    caption = await _caption(leaderboard, {label: 1})

    parser = _parse_caption(caption)
    if extra:
        assert parser.bold_parts == ["Список лидеров по трафику:"]
        assert "... Пропущено записей: 1" in caption
    else:
        assert len(caption.encode("utf-8")) == 1024
        assert parser.bold_parts == ["Список лидеров по трафику:", label]
        assert "Пропущено" not in caption
