from __future__ import annotations

import itertools
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw

from chart import leaderboard_chart
from chart.draw import REG_DOWN, REG_UP, draw_stacked, load_font, nice_ticks
from util import fmt_bytes


@pytest.mark.parametrize("peak", [1, 2, 3])
def test_stacked_small_byte_tick_labels(peak: int) -> None:
    draw = ImageDraw.Draw(Image.new("RGB", (800, 600)))
    face = load_font(14)
    with patch("chart.draw.draw_text") as text:
        draw_stacked(
            draw,
            plot=(100, 100, 700, 500),
            labels=["sample"],
            bottoms=[peak - 1],
            tops=[1],
            bottom_color=REG_DOWN,
            top_color=REG_UP,
            bar_frac=0.5,
            title="Traffic",
            legend=("Down", "Up"),
            empty_label=None,
            fonts=dict.fromkeys(("tick", "title", "body"), face),
        )
    labels = [
        call.args[2]
        for call in text.call_args_list
        if call.args[0] is draw and call.args[1][0] < 100
    ]
    assert labels == [f"{value} B" for value in range(peak + 1)]


@pytest.mark.parametrize("peak", [1, 2, 3])
def test_leaderboard_small_byte_tick_labels(peak: int) -> None:
    with patch("chart.leaderboard.draw_text") as text:
        assert (
            leaderboard_chart(
                {"alice": peak},
                bandwidth_type="total",
                lang={"leaderboard": "Leaderboard", "bw_type_total": "Total"},
            )
            is not None
        )
    labels = [call.args[2] for call in text.call_args_list if call.args[1][1] == 780]
    assert labels == [f"{value} B" for value in range(peak + 1)]


@pytest.mark.parametrize(
    ("peak", "expected"),
    [
        (0, [0.0]),
        (1, [0.0, 1.0]),
        (2, [0.0, 1.0, 2.0]),
        (3, [0.0, 1.0, 2.0, 3.0]),
        (4, [0.0, 1.0, 2.0, 3.0, 4.0]),
        (5, [0.0, 2.0, 4.0, 6.0, 8.0]),
        (1024, [0.0, 500.0, 1000.0, 1500.0, 2000.0]),
        (1_000_000, [0.0, 500_000.0, 1_000_000.0, 1_500_000.0, 2_000_000.0]),
    ],
)
def test_byte_ticks_cover_peak_with_distinct_labels(peak: int, expected: list[float]) -> None:
    ticks = nice_ticks(float(peak))
    assert ticks == expected
    assert ticks[-1] >= peak
    assert all(tick.is_integer() for tick in ticks)
    assert all(right - left >= 1 for left, right in itertools.pairwise(ticks))
    assert len({fmt_bytes(tick) for tick in ticks}) == len(ticks)
