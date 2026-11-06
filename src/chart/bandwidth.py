"""Stacked regular + whitelist bandwidth chart."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

from util import fmt_bytes

from .draw import (
    BG,
    BW_SIZE,
    REG_DOWN,
    REG_UP,
    TEXT,
    TEXT_DIM,
    WL_DOWN,
    WL_UP,
    draw_stacked,
    draw_text,
    load_font,
    save,
)

if TYPE_CHECKING:
    import io
    from collections.abc import Mapping

    from PIL import ImageFont

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
        snapshots: interval deltas, sorted ascending by interval end (ts).
        label: optional, included in the suptitle if provided
        lang: language table, usually taken from `lang_cfg`.
        bar_width: retained for caller compatibility; time bounds determine widths.

    Returns:
        BytesIO containing PNG bytes, positioned at start. None if no data.

    """
    if not snapshots:
        return None
    _ = bar_width  # Interval widths must not be overridden by categorical spacing.

    snaps = sorted(snapshots, key=lambda s: s.ts)
    intervals = [
        (s.start_ts if s.start_ts is not None and s.start_ts <= s.ts else None, s.ts) for s in snaps
    ]
    labels = [str(i + 1) for i in range(len(snaps))]
    reg_up = [s.up for s in snaps]
    reg_down = [s.down for s in snaps]
    wl_up = [s.wl_up for s in snaps]
    wl_down = [s.wl_down for s in snaps]

    width, height = BW_SIZE
    height += 110 + 24 * len(snaps)
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

    def stamp(ts: int) -> str:
        return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")

    first = min(start if start is not None else end for start, end in intervals)
    period = f"UTC {stamp(first)} — {stamp(snaps[-1].ts)}"
    draw_text(draw, (40, 18), header, font=fonts["header"], fill=TEXT)
    draw_text(draw, (40, 48), period, font=fonts["sub"], fill=TEXT_DIM)

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
            bar_frac=1.0,
            title=title,
            legend=legend,
            empty_label=empty,
            fonts=fonts,
            intervals=intervals,
        )
    notes = (
        "UTC intervals: height = total bytes, width = elapsed time (not a rate).",
        "Brackets = known coverage; ? / blank spans = unknown coverage, not zero. End-filtered samples; not an exact rolling total.",
        "Interval [start, end]                                          Duration / type                 Regular / whitelist totals",
    )
    for i, note in enumerate(notes):
        draw_text(draw, (40, 978 + i * 24), note, font=fonts["tick"], fill=TEXT_DIM)
    for i, (snap, (start, end)) in enumerate(zip(snaps, intervals, strict=True)):
        if start is None:
            duration = "unknown"
        else:
            seconds = end - start
            kind = "multi-day" if seconds > 86400 else "partial" if seconds < 86400 else "24h"
            duration = f"{seconds // 3600}h {seconds % 3600 // 60}m {seconds % 60}s / {kind}"
        text = (
            f"{stamp(start) if start is not None else '?'} → {stamp(end)}"
            f"     {duration}     {fmt_bytes(snap.up + snap.down)} / {fmt_bytes(snap.wl_up + snap.wl_down)}"
        )
        draw_text(draw, (40, 1054 + i * 24), text, font=fonts["tick"], fill=TEXT)
    return save(image)
