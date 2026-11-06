from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw

from chart import bandwidth_chart
from chart.draw import REG_DOWN, draw_stacked, load_font
from custom_types import BandwidthSnapshot

if TYPE_CHECKING:
    from PIL import ImageFont


def test_chart_discloses_unknown_partial_multiday_intervals() -> None:
    end = int(datetime(2026, 11, 2, tzinfo=UTC).timestamp())
    snaps = [
        BandwidthSnapshot(ts=end - 60, down=10),
        BandwidthSnapshot(ts=end, start_ts=end - 60, down=20),
        BandwidthSnapshot(ts=end + 3 * 86400, start_ts=end, down=300),
    ]
    lang: dict[str, str] = dict.fromkeys(
        ("bandwidth", "download", "upload", "regular_traffic", "whitelist_traffic", "no_data"),
        "traffic",
    )
    with (
        patch("chart.bandwidth.draw_text") as text,
        patch("chart.bandwidth.draw_stacked") as stacked,
    ):
        assert bandwidth_chart(list(reversed(snaps)), lang=lang) is not None
    rendered = "\n".join(str(call.args[2]) for call in text.call_args_list)
    assert "? → 2026-11-01 23:59:00" in rendered
    assert "2026-11-01 23:59:00 → 2026-11-02 00:00:00" in rendered
    assert "0h 1m 0s / partial" in rendered
    assert "72h 0m 0s / multi-day" in rendered
    assert "not an exact rolling total" in rendered
    assert "3 days" not in rendered
    assert stacked.call_args_list[0].kwargs["intervals"] == [
        (None, end - 60),
        (end - 60, end),
        (end, end + 3 * 86400),
    ]
    assert stacked.call_args_list[0].kwargs["bottoms"] == [10, 20, 300]


@pytest.mark.parametrize("first", [0, 100])
def test_interval_geometry_preserves_elapsed_time_gaps_and_unknown_starts(first: int) -> None:
    draw = ImageDraw.Draw(Image.new("RGB", (1200, 600)))
    face = load_font(14)
    fonts: dict[str, ImageFont.FreeTypeFont] = dict.fromkeys(("tick", "title", "body"), face)
    with (
        patch.object(draw, "rectangle", wraps=draw.rectangle) as rectangle,
        patch.object(draw, "line", wraps=draw.line) as line,
    ):
        draw_stacked(
            draw,
            plot=(100, 100, 1100, 500),
            labels=["1", "2", "3"],
            bottoms=[first, 200, 300],
            tops=[0, 0, 0],
            bottom_color=REG_DOWN,
            top_color="#ffffff",
            bar_frac=0.5,
            title="Intervals",
            legend=("Down", "Up"),
            empty_label=None,
            fonts=fonts,
            intervals=[(0, 100), (400, 900), (None, 1000)],
        )
    bars = [
        call.args[0] for call in rectangle.call_args_list if call.kwargs.get("fill") == REG_DOWN
    ][: 3 if first else 2]
    expected = [(100, 200)] if first else []
    assert [(box[0], box[2]) for box in bars] == [*expected, (500, 1000), (1100, 1100)]
    assert any(
        call.args[0] == (100, 530, 100, 534, 200, 534, 200, 530) for call in line.call_args_list
    )
