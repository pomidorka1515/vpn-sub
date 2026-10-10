"""Horizontal user leaderboard chart."""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from PIL import Image, ImageDraw

from util import fmt_bytes

from .draw import (
    BG,
    BORDER,
    GRID,
    LB_SIZE,
    PANEL,
    REG_DOWN,
    TEXT,
    TEXT_DIM,
    draw_text,
    ellipsis,
    load_font,
    nice_ticks,
    save,
    text_size,
)

if TYPE_CHECKING:
    import io
    from collections.abc import Mapping


def leaderboard_chart(
    data: dict[str, int],
    *,
    bandwidth_type: Literal["total", "monthly", "wl_monthly"],
    lang: Mapping[str, str],
) -> io.BytesIO | None:
    """Render a leaderboard of users by bandwidth.

    Args:
        data: leaderboard data, usually taken from core.Subscription.leaderboard().
            structure: {"username": 123456, "second_place": 123123}
        bandwidth_type: type of bandwidth.
        lang: language table, usually taken from `lang_cfg`
    """
    if not data:
        return None

    bw_key = f'bw_type_{bandwidth_type}'
    bw_label = lang.get(bw_key, lang['bw_type_total'])
    sorted_users = sorted(data.items(), key=lambda x: x[1], reverse=True)[:15]

    width, height = LB_SIZE
    image = Image.new('RGB', (width, height), BG)
    draw = ImageDraw.Draw(image)
    header_font = load_font(22)
    tick_font = load_font(14)

    header = f'{lang["leaderboard"]} — {bw_label}'
    draw_text(draw, (40, 18), header, font=header_font, fill=TEXT)

    label_col = 220
    value_col = 130
    left = 40 + label_col
    right = width - 36 - value_col
    top = 90
    bottom = height - 70
    draw.rectangle((left, top, right, bottom), fill=PANEL)

    peak = max((v for _, v in sorted_users), default=0)
    ticks = nice_ticks(float(peak))
    scale_max = ticks[-1] if ticks[-1] > 0 else 1.0
    plot_w = right - left

    for tick in ticks:
        x = left + int((tick / scale_max) * plot_w)
        draw.line((x, top, x, bottom), fill=GRID, width=1)
        label = fmt_bytes(tick)
        tw, _ = text_size(tick_font, label)
        draw_text(draw, (x - tw // 2, bottom + 10), label, font=tick_font, fill=TEXT_DIM)

    draw.line((left, bottom, right, bottom), fill=BORDER, width=1)
    draw.line((left, top, left, bottom), fill=BORDER, width=1)

    n = len(sorted_users)
    slot = (bottom - top) / max(n, 1)
    bar_h = max(8, int(slot * 0.62))
    for i, (name, value) in enumerate(sorted_users):
        cy = top + slot * (i + 0.5)
        y0 = int(cy - bar_h / 2)
        y1 = y0 + bar_h
        bar_w = int((value / scale_max) * plot_w) if scale_max else 0
        if bar_w:
            draw.rectangle((left, y0, left + bar_w, y1), fill=REG_DOWN)
        shown = ellipsis(tick_font, name, label_col - 16)
        tw, th = text_size(tick_font, shown)
        draw_text(draw, (left - 12 - tw, int(cy - th / 2)), shown, font=tick_font, fill=TEXT_DIM)
        value_label = fmt_bytes(value)
        draw_text(draw, (left + bar_w + 8, int(cy - th / 2)), value_label, font=tick_font, fill=TEXT_DIM)

    return save(image)
