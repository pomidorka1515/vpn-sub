from __future__ import annotations

from typing import TYPE_CHECKING

from PIL import Image

from chart import bandwidth_chart, leaderboard_chart
from custom_types import BandwidthSnapshot

if TYPE_CHECKING:
    import io

_LANG = {
    "bandwidth": "Использование трафика",
    "day": "день",
    "days": "дн.",
    "regular_traffic": "Обычный трафик",
    "whitelist_traffic": "Белый список",
    "download": "Загрузка",
    "upload": "Отдача",
    "no_data": "Нет данных",
    "leaderboard": "Таблица лидеров",
    "bw_type_total": "весь трафик",
    "bw_type_monthly": "трафик за месяц",
    "bw_type_wl_monthly": "WL-трафик за месяц",
}


def _png(buf: io.BytesIO | None, size: tuple[int, int]) -> Image.Image:
    assert buf is not None
    assert buf.tell() == 0
    raw = buf.getvalue()
    assert raw.startswith(b"\x89PNG")
    image = Image.open(buf)
    assert image.size == size
    assert image.format == "PNG"
    return image


def test_bandwidth_chart_empty_returns_none() -> None:
    assert bandwidth_chart([], lang=_LANG) is None


def test_bandwidth_chart_returns_png() -> None:
    snaps = [
        BandwidthSnapshot(ts=1_700_000_000 + i * 86_400, up=i * 1_000, down=i * 2_000, wl_up=100, wl_down=200)
        for i in range(3)
    ]
    image = _png(bandwidth_chart(snaps, label="Алиса", lang=_LANG), (1400, 980))
    assert image.mode == "RGB"


def test_bandwidth_chart_all_zero_still_renders() -> None:
    snaps = [BandwidthSnapshot(ts=1_700_000_000)]
    _png(bandwidth_chart(snaps, lang=_LANG), (1400, 980))


def test_bandwidth_chart_bar_width_override() -> None:
    snaps = [BandwidthSnapshot(ts=1_700_000_000, up=10, down=20)]
    _png(bandwidth_chart(snaps, lang=_LANG, bar_width=0.5), (1400, 980))


def test_bandwidth_chart_many_days() -> None:
    snaps = [
        BandwidthSnapshot(ts=1_700_000_000 + i * 86_400, down=1_000_000 * (i + 1))
        for i in range(40)
    ]
    _png(bandwidth_chart(snaps, lang=_LANG), (1400, 980))


def test_leaderboard_chart_empty_returns_none() -> None:
    assert leaderboard_chart({}, bandwidth_type="total", lang=_LANG) is None


def test_leaderboard_chart_returns_png() -> None:
    data = {"alice": 1_500_000, "bob": 900_000, "ёлка": 10}
    image = _png(leaderboard_chart(data, bandwidth_type="monthly", lang=_LANG), (1260, 840))
    assert image.mode == "RGB"


def test_leaderboard_chart_caps_rows() -> None:
    data = {f"user-{i:02d}": (20 - i) * 1_000_000 for i in range(20)}
    _png(leaderboard_chart(data, bandwidth_type="wl_monthly", lang=_LANG), (1260, 840))


def test_leaderboard_chart_long_name() -> None:
    data = {"a" * 80: 5_000_000_000, "b": 1}
    _png(leaderboard_chart(data, bandwidth_type="total", lang=_LANG), (1260, 840))
