"""Stacked regular + whitelist bandwidth chart."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont

from .draw import (
    BG,
    BW_SIZE,
    REG_DOWN,
    REG_UP,
    TEXT,
    TEXT_DIM,
    WL_DOWN,
    WL_UP,
    calc_bar_width,
    draw_stacked,
    draw_text,
    load_font,
    save,
)

if TYPE_CHECKING:
    import io
    from collections.abc import Mapping

    from custom_types import BandwidthSnapshot


def bandwidth_chart(
    snapshots: list[BandwidthSnapshot],
    *,
    label: str | None = None,
    lang: Mapping[str, str],
    bar_width: float | None = None,
) -> io.BytesIO | None:
    """Render two stacked bar charts (regular + whitelist) into a single PNG.

    Args:
        snapshots: daily bandwidth records, will be sorted ascending by ts
        label: optional, included in the suptitle if provided
        lang: language table, usually taken from `lang_cfg`.
        bar_width: optional, bar width override. Defaults to auto-calculated
            based on number of data points to maintain good visual density.

    Returns:
        BytesIO containing PNG bytes, positioned at start. None if no data.
    """
    if not snapshots:
        return None

    snaps = sorted(snapshots, key=lambda s: s.ts)
    labels = [datetime.fromtimestamp(s.ts, tz=UTC).strftime("%m/%d") for s in snaps]
    reg_up = [s.up for s in snaps]
    reg_down = [s.down for s in snaps]
    wl_up = [s.wl_up for s in snaps]
    wl_down = [s.wl_down for s in snaps]

    width, height = BW_SIZE
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)
    fonts: dict[str, ImageFont.FreeTypeFont] = {
        "header": load_font(22),
        "sub": load_font(16),
        "title": load_font(18),
        "body": load_font(16),
        "tick": load_font(14),
    }

    header = f"{lang['bandwidth']} — {label}" if label else lang["bandwidth"]
    period = f"{len(snaps)} {lang['day']}" if len(snaps) == 1 else f"{len(snaps)} {lang['days']}"
    draw_text(draw, (40, 18), header, font=fonts["header"], fill=TEXT)
    draw_text(draw, (40, 48), period, font=fonts["sub"], fill=TEXT_DIM)

    n = len(labels)
    bar_frac = calc_bar_width(n) if bar_width is None else bar_width
    legend = (lang["download"], lang["upload"])
    margin_l, margin_r = 110, 36
    panels = (
        (lang["regular_traffic"], reg_down, reg_up, REG_DOWN, REG_UP, 130, 500),
        (lang["whitelist_traffic"], wl_down, wl_up, WL_DOWN, WL_UP, 580, 930),
    )
    for title, bottoms, tops, bottom_color, top_color, top, bottom in panels:
        empty = lang["no_data"] if not any(bottoms) and not any(tops) else None
        draw_stacked(
            draw,
            plot=(margin_l, top, width - margin_r, bottom),
            labels=labels,
            bottoms=bottoms,
            tops=tops,
            bottom_color=bottom_color,
            top_color=top_color,
            bar_frac=bar_frac,
            title=title,
            legend=legend,
            empty_label=empty,
            fonts=fonts,
        )
    return save(image)
